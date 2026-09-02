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
    # built in-process (see _BUILDERS) — the HTTP path only ever sees the
    # anonymous tier. Path kept for the manifest's source_path field.
    ("facilities", "/api/v1/facilities?limit=50000",
     "Data-center facility inventory — public fields, all rows"),
    # built in-process (see _BUILDERS) — the HTTP page is 50 of 13,009.
    ("news", "/api/v1/news",
     "Industry news LINK INDEX — headlines, URLs, attribution (no article text)"),
    ("ai-capacity", "/ai-capacity-index/today.json", "AI capacity index (daily)"),
]
_NAMES = {d[0] for d in DATASETS}

# ── ★ THE PUBLIC FACILITY CORPUS (2026-09-02, owner-decided) ────────────────
#
# The stub guard below refuses to publish a tier preview. That left `facilities`
# blocked and correctly so — but blocked is not the answer, it is the absence
# of one. The answer, decided by the owner on 2026-09-02:
#
#   BULK CARRIES EXACTLY THE FIELDS ALREADY PUBLISHED ON EVERY PUBLIC
#   dchub.cloud/facilities/<slug> PAGE, FOR EVERY ROW.
#
# name, provider, city, state, country, slug, profile_url — main.py's
# BASIC_FIELDS, the same set the free API already returns (for 5 rows) and the
# same set the crawlable profile pages already render (for all of them). NO
# field becomes public that was not already public; only the SHAPE changes.
# capacity, coordinates, tenants, specs and every scored field stay paid and
# are not selectable here — the query names its columns, so a paid field
# cannot arrive by accident the way the tier preview arrived by accident.
#
# ★ WHAT THIS DOES CHANGE, STATED PLAINLY: crawl economics. One fetch replaces
# 20,191 page loads. The fields were always reachable; the cost of taking all
# of them was not. That was the trade accepted when this was chosen, and it is
# recorded here rather than discovered later by whoever asks why.
#
# ★ ROW BASIS IS THE CITEABLE ONE, VERBATIM. public_endpoints.py publishes
# facilities as COUNT(DISTINCT canonical_slug) FROM discovered_facilities
# WHERE canonical_slug IS NOT NULL, and /api/agent/stats calls that "the
# citeable figure". This query mirrors it exactly so the export's row count
# RECONCILES with the number on the homepage. An export that disagrees with
# the published total is a new inconsistency dressed as a fix.
_PUBLIC_FACILITY_SQL = """
SELECT DISTINCT ON (canonical_slug)
       canonical_slug, name, provider, city, state, country
  FROM discovered_facilities
 WHERE canonical_slug IS NOT NULL
 ORDER BY canonical_slug, confidence_score DESC NULLS LAST
"""

# Per-layer terms, the same split summarize_for_citation publishes. Stamped ON
# the payload because a bulk file outlives the page that described it.
_FACILITY_LICENCE = {
    "layer": "facility_inventory",
    "terms": "Composite — see https://dchub.cloud/data-sources",
    "attribution": "DC Hub (dchub.cloud)",
    "note": ("per-source terms differ and some are share-alike (ODbL 1.0); "
             "this file may NOT be relicensed as CC-BY. Fields are limited to "
             "those already published on the public facility pages."),
}


def _build_public_facilities():
    """The public facility corpus as an export payload, or None if the DB is
    unreachable (the caller then reports it as a normal dataset failure).

    Reads the columns it names and nothing else — there is no SELECT * here on
    purpose.
    """
    try:
        from db_utils import get_read_db
    except Exception:
        return None
    conn = None
    try:
        conn = get_read_db()
        cur = conn.cursor()
        cur.execute(_PUBLIC_FACILITY_SQL)
        rows = cur.fetchall() or []
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    out = []
    for r in rows:
        # ★ 2026-09-02, found in the first published build. canonical_slug is
        # filtered IS NOT NULL, which is NOT the same as non-empty: one row of
        # 20,194 (`L7`, Kyiv) carries the EMPTY STRING and rendered
        # "https://dchub.cloud/facilities/" — a dead link shipped inside a
        # citeable corpus.
        #
        # ★ THE ROW STAYS. Tightening the WHERE clause to <> '' would drop it,
        # make the export 20,193 and break the reconciliation that is the whole
        # point of mirroring public_endpoints' basis verbatim (export records
        # == /api/agent/stats.total_facilities == 20,194, measured). The count
        # is right; only the URL was wrong. So the URL goes null and the row is
        # counted — null says "this facility has no page", which is true, where
        # a bare directory URL said "here is its page", which was not.
        slug = (r[0] or "").strip()
        out.append({
            "slug": slug,
            "name": r[1],
            "provider": r[2],
            "city": r[3],
            "state": r[4],
            "country": r[5],
            "profile_url": ("https://dchub.cloud/facilities/" + slug
                            if slug else None),
        })
    return {
        "dataset": "facilities",
        "count": len(out),
        "basis": ("DISTINCT canonical_slug over discovered_facilities WHERE "
                  "canonical_slug IS NOT NULL — distinct BUILDINGS, the same "
                  "query behind the published facilities total"),
        "fields": ["slug", "name", "provider", "city", "state", "country",
                   "profile_url"],
        "fields_note": ("exactly the fields already rendered on the public "
                        "facility pages. Capacity, coordinates, tenants and "
                        "scored fields are NOT in this export and are not "
                        "free"),
        "licence": _FACILITY_LICENCE,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "data": out,
    }


# Datasets built in-process rather than fetched over localhost, because the
# HTTP path can only ever see what an anonymous caller sees.
# ── ★ THE NEWS LINK INDEX (2026-09-02) ──────────────────────────────────────
#
# /api/v1/news returns 50 of 13,009 — caught by the stub guard's count test
# alone, with NO upsell markers on the payload, which is why it survived three
# months looking like a real 35KB dataset. It is the third stub the guard
# found and the only one nobody had suspected.
#
# ★ THIS EXPORT DELIBERATELY DROPS TWO FIELDS THE API RETURNS.
#   · summary    news_engine.py:290 builds it from
#                `entry.get('summary', entry.get('description',''))` — the
#                PUBLISHER'S OWN TEXT, lifted from their feed. One summary
#                beside a link is ordinary feed behaviour; 13,009 of them in a
#                downloadable file is republishing someone else's writing at
#                scale, and DC Hub does not hold that right.
#   · image_url  points at the publisher's server. Shipping 13,009 of them
#                invites bulk hotlinking of assets we do not host or pay for.
#
# What ships is the link-and-fact layer: title, url, source, published_at,
# category, relevance_score. A headline plus a link plus an attribution is how
# an index works, and relevance_score is ours. An ingester that wants the
# article reads it from the publisher, which is also the outcome the publisher
# wants — this file sends traffic to them rather than standing in for them.
_NEWS_SQL = """
SELECT id, title, url, source, published_at, category, relevance_score,
       publisher_url
  FROM news_articles
 WHERE url IS NOT NULL AND url <> ''
 ORDER BY published_at DESC NULLS LAST
"""

# Same statement without publisher_url, for a database that has not taken the
# ALTER yet. The export must not vanish because one column is new.
_NEWS_SQL_LEGACY = _NEWS_SQL.replace(",\n       publisher_url", "")

# Hosts that are aggregators, not publishers. A row whose `url` points here is
# a link TO a link: following it lands on a Google interstitial, not on the
# article. Declared per row so a downstream index can tell the two apart
# instead of discovering it by clicking.
_AGGREGATOR_HOSTS = ("news.google.com",)


def _is_aggregator(url):
    u = (url or "").lower()
    return any(("://" + h) in u or ("//" + h) in u for h in _AGGREGATOR_HOSTS)

_NEWS_LICENCE = {
    "layer": "news_index",
    "terms": "link index — headlines, URLs and attribution only",
    "attribution": "DC Hub (dchub.cloud); each item credits its own source",
    "note": ("article TEXT is NOT included and is not DC Hub's to license: "
             "`summary` and `image_url` are the publisher's and are excluded "
             "from this export by design. Follow `url` to the source"),
}


def _build_news_index():
    """The news link index, or None if the DB is unreachable."""
    try:
        from db_utils import get_read_db
    except Exception:
        return None
    conn = None
    try:
        conn = get_read_db()
        cur = conn.cursor()
        try:
            cur.execute(_NEWS_SQL)
            rows = cur.fetchall() or []
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            cur = conn.cursor()
            cur.execute(_NEWS_SQL_LEGACY)
            rows = [tuple(r) + (None,) for r in (cur.fetchall() or [])]
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    out = []
    for r in rows:
        pub = r[4]
        out.append({
            "id": r[0],
            "title": r[1],
            "url": r[2],
            "source": r[3],
            # isoformat when the driver hands back a datetime, str otherwise —
            # a raw datetime is not JSON-serialisable and would fail the whole
            # build at json.dumps, not here where it could be diagnosed.
            "published_at": (pub.isoformat() if hasattr(pub, "isoformat")
                             else (str(pub) if pub is not None else None)),
            "category": r[5],
            "relevance_score": r[6],
            # Who actually published it. For the 70% of rows whose `url` is an
            # opaque Google News token this is the only attribution there is.
            "publisher_url": r[7],
            "url_is_aggregator": _is_aggregator(r[2]),
        })
    return {
        "dataset": "news",
        "count": len(out),
        "basis": ("news_articles WHERE url IS NOT NULL AND url <> '', newest "
                  "first — every indexed article, not the 50-item API page"),
        "fields": ["id", "title", "url", "source", "published_at", "category",
                   "relevance_score", "publisher_url", "url_is_aggregator"],
        "fields_note": ("summary and image_url are deliberately NOT exported "
                        "— see licence.note. Where url_is_aggregator is true "
                        "the `url` is a Google News token that resolves only "
                        "in a browser; `publisher_url` names the publisher and "
                        "is the attribution to use"),
        "licence": _NEWS_LICENCE,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "data": out,
    }


_BUILDERS = {"facilities": _build_public_facilities,
             "news": _build_news_index}



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
            _builder = _BUILDERS.get(name)
            if _builder is not None:
                _payload = _builder()
                if _payload is None:
                    failed.append({"name": name,
                                   "error": "builder returned no rows (DB unreachable?)"})
                    continue
                raw = json.dumps(_payload).encode()
            else:
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
