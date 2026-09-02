"""
r2_exports.py — #2 (2026-06-08): publish key datasets to R2 Object Storage for
bulk download + large-dataset failover.

Fetches a curated set of internal endpoints, gzips them, uploads to R2 (zero
egress). Two payoffs:
  • "Download the dataset" — agents / researchers / customers pull full datasets
    directly from R2 via short-lived presigned URLs. No backend bandwidth, no R2
    egress charge. A discovery magnet + a Pro/Enterprise export feature.
  • Large-dataset failover — payloads too big for KV's 25MB/value limit live in
    R2; a worker can serve them on a Railway outage.

Routes:
  POST/GET /api/v1/admin/exports/build  (admin/cron/probe-gated) — (re)build all
  GET      /api/v1/exports              (public) — manifest of available exports
  GET      /api/v1/exports/<name>       (public) — 302 → presigned R2 URL (1h)
"""
import os
import gzip
import json
import datetime
import urllib.request
from flask import Blueprint, jsonify, request, redirect, make_response

r2_exports_bp = Blueprint("r2_exports", __name__)

# NOTE: default to dchub-daily (writable, actively used) — NOT R2_BUCKET_NAME,
# which on Railway is dchub-backups (the locked-down DB-backup bucket → writes
# 401 Unauthorized). Override with R2_EXPORTS_BUCKET if you want a dedicated one.
R2_BUCKET = os.environ.get("R2_EXPORTS_BUCKET") or "dchub-daily"
R2_PREFIX = "exports/"
_PORT = os.environ.get("PORT", "8080")
_BASE = "http://127.0.0.1:" + str(_PORT)

# (name, internal_path, description). FREE/PUBLIC datasets ONLY — these become
# public presigned downloads, so paid endpoints (grid intelligence, pipeline,
# fiber, analyze_site) are deliberately excluded; exporting them would give away
# paid data. Fetched via localhost so existing serialization applies; per-dataset
# failures skip + report, so a bad path never breaks the build. Extend freely.
DATASETS = [
    ("agent-registry", "/api/agents/registry", "Connected AI platforms + activity"),
    ("markets", "/api/v1/markets", "All tracked data-center markets"),
    ("facilities", "/api/v1/facilities?limit=50000", "Data-center facilities (public set)"),
    ("news", "/api/v1/news", "Curated industry news feed"),
    ("ai-capacity", "/ai-capacity-index/today.json", "AI capacity index (daily)"),
]
_NAMES = {d[0] for d in DATASETS}


def _r2():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT_URL", ""),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        region_name="auto",
    )


def _r2_ready():
    return bool(os.environ.get("R2_ENDPOINT_URL") and os.environ.get("R2_ACCESS_KEY_ID"))


def _authorized():
    admin = os.environ.get("DCHUB_ADMIN_KEY", "")
    if admin and request.headers.get("X-Admin-Key") == admin:
        return True
    ik = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("DCHUB_SYNC_KEY")
    if ik and request.headers.get("X-Internal-Key") == ik:
        return True
    if (request.headers.get("X-DC-Probe") or "").lower() == "exports-build":
        return True
    return False


def _fetch(path):
    req = urllib.request.Request(
        _BASE + path,
        headers={"User-Agent": "DCHub-R2Exporter/1.0", "X-DC-Probe": "exports-build"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# ── ★ THE STUB GUARD (2026-09-02) ───────────────────────────────────────────
#
# WHAT SHIPPED FOR THREE MONTHS. _fetch() calls these paths over localhost with
# NO api_key, so a tier-gated endpoint answers it the way it answers any
# anonymous caller: with the free preview. The builder gzipped that preview and
# published it under the dataset's full description. Measured 2026-09-02 on the
# live manifest:
#
#   facilities  "Data-center facilities (public set)"  count 5, total_matching
#               20,191, tier 'free', full_results_available true, upgrade_url
#   markets     "All tracked data-center markets"      count 5 of 300+,
#               locked, upsell, signup_url
#
# 2,907 raw bytes offered as the facility corpus. Nothing failed and nothing
# logged: a 200 with a paywall envelope in it is indistinguishable from a 200
# with data unless something looks INSIDE the body, which nothing did.
#
# ★ THE GUARD REFUSES, IT DOES NOT REPAIR. Authenticating the fetch would make
# these datasets full — and would publish a paid, ODbL-composite corpus at a
# public presigned URL as a side effect of a bug fix. What a free bulk caller
# may download is an owner decision with a licence in it, not a default this
# module gets to pick. So a gated payload is NOT uploaded, the dataset stays in
# the manifest marked unusable with the evidence attached, and /exports/<name>
# refuses to serve it. The gap is published rather than closed.
_GATE_MARKERS = ("upgrade_url", "upsell", "signup_url", "locked",
                 "full_results_available")


def _row_count(d):
    """Rows in a payload, without knowing its shape: the longest top-level
    list, or the list itself. None when neither applies."""
    if isinstance(d, list):
        return len(d)
    if isinstance(d, dict):
        lens = [len(v) for v in d.values() if isinstance(v, list)]
        if lens:
            return max(lens)
    return None


def _gate_evidence(raw):
    """Evidence that this payload is a TIER PREVIEW, or None if it is data.

    Two independent tests, because either alone has a false negative: marker
    keys catch an envelope that happens to be complete, and the count/total
    comparison catches a trimmed payload whose upsell keys were renamed.
    Unparseable bytes are NOT evidence — an opaque body is a different failure
    and must not be reported as a paywall.
    """
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not isinstance(d, (dict, list)):
        return None
    markers = ([k for k in _GATE_MARKERS if k in d] if isinstance(d, dict)
               else [])
    if isinstance(d, dict) and str(d.get("tier", "")).lower() == "free":
        markers.append("tier=free")
    n = _row_count(d)
    total = None
    if isinstance(d, dict):
        for k in ("total_matching", "total", "total_available"):
            if isinstance(d.get(k), int):
                total = d[k]
                break
    truncated = (n is not None and total is not None and n < total)
    if not markers and not truncated:
        return None
    return {"markers": sorted(set(markers)), "records": n,
            "total_available": total, "truncated": bool(truncated)}


def _as_of(raw, fallback):
    """The payload's own freshness stamp when it has one — Mistral and any
    other no-egress ingester needs to know WHEN, not just WHAT. Falls back to
    build time, labelled as such by the caller."""
    try:
        d = json.loads(raw)
    except Exception:
        return None, "unparseable"
    if isinstance(d, dict):
        for k in ("as_of", "generated_at", "computed_at", "last_updated"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v, k
    return fallback, "build_time"


# Per-dataset cache lifetime, published so an ingester can schedule a refetch
# instead of polling. Conservative: shorter than the real cadence, never longer.
_TTL_SECONDS = {"agent-registry": 3600, "markets": 86400,
                "facilities": 86400, "news": 3600, "ai-capacity": 86400}


@r2_exports_bp.route("/api/v1/admin/exports/build", methods=["POST", "GET"])
def build():
    if not _authorized():
        return jsonify({"ok": False, "error": "admin_required"}), 403
    if not _r2_ready():
        return jsonify({"ok": False, "error": "R2 not configured (R2_ENDPOINT_URL/KEY missing)"}), 503
    s3 = _r2()
    built, failed, blocked = [], [], []
    manifest = {"generated_at": datetime.datetime.utcnow().isoformat() + "Z", "datasets": []}
    for name, path, desc in DATASETS:
        try:
            raw = _fetch(path)
            key = R2_PREFIX + name + ".json.gz"
            gate = _gate_evidence(raw)
            if gate is not None:
                # NOT uploaded. The previous object (if any) is left in place
                # but download() will refuse to hand it out, so the stub stops
                # being reachable without deleting anything.
                blocked.append(name)
                manifest["datasets"].append({
                    "name": name, "description": desc,
                    "usable": False,
                    "blocked_by": "tier_gate",
                    "evidence": gate,
                    "means": (
                        "the exporter fetched this path unauthenticated and "
                        "got the free-tier preview, not the dataset. It is NOT "
                        "published: a preview served under this description "
                        "would misrepresent "
                        + (str(gate.get("records")) if gate.get("records")
                           is not None else "a handful of")
                        + " rows as the full set. What a free bulk caller may "
                        "download is an owner decision, not a default"),
                    "download": None,
                })
                continue
            stamp, stamp_source = _as_of(raw, manifest["generated_at"])
            gz = gzip.compress(raw, compresslevel=6)
            s3.put_object(Bucket=R2_BUCKET, Key=key, Body=gz,
                          ContentType="application/json", ContentEncoding="gzip")
            built.append(name)
            manifest["datasets"].append({
                "name": name, "key": key, "description": desc,
                "usable": True,
                "bytes_gz": len(gz), "bytes_raw": len(raw),
                "records": _row_count(json.loads(raw)),
                "as_of": stamp,
                "as_of_source": stamp_source,
                "ttl_seconds": _TTL_SECONDS.get(name),
                "source_path": path,
                "download": "https://dchub.cloud/api/v1/exports/" + name,
            })
        except Exception as e:
            failed.append({"name": name, "error": type(e).__name__ + ": " + str(e)[:100]})
    try:
        s3.put_object(Bucket=R2_BUCKET, Key=R2_PREFIX + "manifest.json",
                      Body=json.dumps(manifest).encode(), ContentType="application/json")
    except Exception:
        pass
    manifest["usable_datasets"] = len(built)
    manifest["blocked_datasets"] = len(blocked)
    return jsonify({"ok": True, "built": built, "blocked": blocked,
                    "failed": failed, "bucket": R2_BUCKET,
                    "manifest": manifest}), (200 if not (failed or blocked)
                                             else 207)


@r2_exports_bp.route("/api/v1/exports", methods=["GET"])
def list_exports():
    if not _r2_ready():
        return jsonify({"exports": [], "note": "R2 not configured"}), 200
    try:
        s3 = _r2()
        obj = s3.get_object(Bucket=R2_BUCKET, Key=R2_PREFIX + "manifest.json")
        return jsonify(json.loads(obj["Body"].read())), 200
    except Exception:
        return jsonify({"datasets": [],
                        "note": "no exports built yet — POST /api/v1/admin/exports/build"}), 200


@r2_exports_bp.route("/api/v1/exports/<name>", methods=["GET"])
def download(name):
    if name not in _NAMES:
        return jsonify({"error": "unknown dataset", "available": sorted(_NAMES)}), 404
    if not _r2_ready():
        return jsonify({"error": "R2 not configured"}), 503
    try:
        s3 = _r2()
        # ★ A dataset the last build refused to publish must not be reachable
        # through the redirect either — an older stub object can still be
        # sitting in the bucket, and 302-ing to it would serve exactly the
        # preview the build declined to ship. Manifest unreadable => serve as
        # before (fail-open: a manifest hiccup must not take downloads down).
        try:
            _m = json.loads(s3.get_object(
                Bucket=R2_BUCKET,
                Key=R2_PREFIX + "manifest.json")["Body"].read())
            for _d in (_m.get("datasets") or []):
                if _d.get("name") == name and _d.get("usable") is False:
                    return jsonify({
                        "error": "dataset_not_published",
                        "dataset": name,
                        "blocked_by": _d.get("blocked_by"),
                        "evidence": _d.get("evidence"),
                        "means": _d.get("means"),
                    }), 409
        except Exception:
            pass
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": R2_PREFIX + name + ".json.gz"},
            ExpiresIn=3600,
        )
        return redirect(url, code=302)
    except Exception as e:
        return jsonify({"error": type(e).__name__}), 502


# ── Enterprise data-license: the DEEP named-operator facility census ─────────
# r-data-license (2026-06-22): the public exports above are FREE/PUBLIC + trimmed.
# THIS is the paid moat asset — the deep per-facility census enterprise buyers
# license. Gated to enterprise tier via require_plan, which FAILS CLOSED (401 no
# auth / 403 wrong tier / 503 if gating unavailable) — the gate is load-bearing
# because this exposes the crown-jewel data. NOT in the free EXPORTS manifest.
#
# Data quality IS the product (an enterprise buyer spot-checks). Gates verified
# against live discovered_facilities (21,808 rows):
#   • EXCLUDE provider IN ('', 'Unknown')  (2,653 anonymous rows)
#   • EXCLUDE is_duplicate = 1
#   • NORMALIZE corporate-suffix dups ("Equinix, Inc." → "Equinix")
#   • DROP sqft (dead: 3/21,808 populated)
# Net ≈ 19,155 named-operator facilities with deep fields.
try:
    from api_tier_gating import require_plan as _license_require_plan
except Exception as _lic_e:  # pragma: no cover — FAIL CLOSED if gating absent
    def _license_require_plan(_plan):
        def _decorator(fn):
            def _wrapped(*a, **kw):
                return jsonify(ok=False, error="gate_not_wired",
                               hint="Tier-gating unavailable; enterprise export fails closed."), 503
            _wrapped.__name__ = getattr(fn, "__name__", "_wrapped")
            return _wrapped
        return _decorator

import csv as _csv
import io as _io
import re as _re

# Collapse the corporate-suffix dup ("Equinix, Inc." → "Equinix") WITHOUT touching
# names that lack the comma-suffix pattern (conservative — only strips a trailing
# ", <suffix>"). This is the observed dedup source, not aggressive name munging.
_LIC_SUFFIX_RE = _re.compile(
    r',\s*(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|'
    r'gmbh|s\.a|s\.a\.s|ag|n\.v|b\.v|plc|holdings?)\.?\s*$', _re.I)

def _lic_norm_provider(p):
    if not p:
        return p
    out = _LIC_SUFFIX_RE.sub('', p).strip()
    return out or p

_LICENSE_COLS = ["name", "provider", "market", "city", "state", "country",
                 "latitude", "longitude", "power_mw", "status", "facility_type",
                 "source_url", "operational_year", "investment_usd"]


@r2_exports_bp.route("/api/v1/license/facilities", methods=["POST"])
@_license_require_plan('enterprise')
def license_facilities():
    """Enterprise data-license: the deep NAMED-OPERATOR data-center facility census.
    POST-only — the CF edge cacheEverything's /api/* GETs by URL (ignoring auth +
    no-store), which would cache an enterprise response and serve it to a
    non-enterprise caller (a gate bypass — confirmed in testing). POST is never
    edge-cached, so it sidesteps that leak entirely. Enterprise consumers POST
    with their X-API-Key. Query params: ?format=csv|json (default csv), ?limit=N
    (cap 50000). Data-quality gated. CC-BY-4.0 — cite "DC Hub (dchub.cloud)"."""
    fmt = (request.args.get("format") or "csv").lower()
    try:
        limit = min(int(request.args.get("limit", 0) or 0), 50000)
    except Exception:
        limit = 0
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return jsonify(ok=False, error="no_db"), 503
    sql = (
        "SELECT name, provider, market, city, state, country, latitude, longitude, "
        "power_mw, status, facility_type, source_url, operational_year, investment_usd "
        "FROM discovered_facilities "
        "WHERE provider IS NOT NULL AND btrim(provider) NOT IN ('', 'Unknown') "
        "AND COALESCE(is_duplicate, 0) = 0 "
        "ORDER BY provider, market NULLS LAST, name"
    )
    if limit:
        sql += " LIMIT %d" % limit
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, sslmode="require", connect_timeout=10)
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        return jsonify(ok=False, error="query_failed", detail=str(e)[:200]), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    # Normalize the provider (index 1) for the dedup; drop nothing else.
    norm = []
    for r in rows:
        r = list(r)
        r[1] = _lic_norm_provider(r[1])
        norm.append(r)
    if fmt == "json":
        out = make_response(jsonify(ok=True, source="DC Hub (dchub.cloud)", license="CC-BY-4.0",
                                    count=len(norm), columns=_LICENSE_COLS,
                                    facilities=[dict(zip(_LICENSE_COLS, r)) for r in norm]))
    else:
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(_LICENSE_COLS)
        for r in norm:
            w.writerow(["" if v is None else v for v in r])
        out = make_response(buf.getvalue())
        out.headers["Content-Type"] = "text/csv; charset=utf-8"
        out.headers["Content-Disposition"] = 'attachment; filename="dchub_facility_census.csv"'
    # MOAT DATA — never cache. The CF edge cacheEverything's /api/* GETs by URL
    # (ignoring auth headers), so a cached enterprise response could be served to
    # a non-enterprise caller = a gate bypass. no-store + private + Vary on the
    # auth headers is the in-origin defense; the edge must also be confirmed not
    # to cache this path (verify test).
    out.headers["Cache-Control"] = "no-store, private, max-age=0"
    out.headers["Vary"] = "Authorization, X-API-Key"
    out.headers["X-DCHub-License"] = "CC-BY-4.0; cite DC Hub (dchub.cloud)"
    return out
