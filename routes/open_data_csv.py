"""
open_data_csv.py — Phase MM (2026-05-15) Tier 1 #3: open data CSVs.

Public CSV exports with an attribution requirement embedded in a
LICENSE.txt-equivalent header. Researchers, bloggers, AI training
pipelines all ingest. Every download becomes an attribution somewhere
that compounds for SEO + brand authority for years.

Endpoints:
    GET /api/v1/open-data/manifest.json   — list of available datasets
    GET /api/v1/open-data/<slug>.csv      — streams one dataset
    GET /open-data                        — public landing page (served
                                            statically; this endpoint
                                            only delivers data)

Each CSV starts with comment lines that include:
    # Source: DC Hub (https://dchub.cloud) — Updated: YYYY-MM-DD
    # License: CC-BY-4.0 — Attribution required: "Source: dchub.cloud"
    # Citation: DC Hub, "<dataset name>", 2026.

Sized to be useful but not abusive:
    facilities-2026.csv    ~12,500 rows, top fields
    dcpi-markets-2026.csv  ~276 rows, full DCPI signal
    pipeline-2026.csv      ~1,000 rows, capacity pipeline
    isos-2026.csv          11 ISOs with snapshot stats
"""
import csv
import io
import os
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify

open_data_csv_bp = Blueprint("open_data_csv", __name__)


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=10)
    c.autocommit = True
    return c


_LICENSE_HEADER = (
    "# DC Hub Open Data — {dataset}\n"
    "# Source: https://dchub.cloud · Updated: {date}\n"
    "# License: CC-BY-4.0 — Attribution required.\n"
    "# Cite as: DC Hub. \"{dataset}\". {year}. https://dchub.cloud/open-data\n"
    "# Contact: hello@dchub.cloud · Schema: see header row below.\n"
    "#\n"
)


def _stream_csv(dataset_name, query, header_row, row_transform=None):
    """Stream a query result as a CSV with a DC Hub attribution header."""
    today = datetime.now(timezone.utc)
    year = today.year
    date_str = today.strftime("%Y-%m-%d")

    def generate():
        # Header comments (License + citation)
        yield _LICENSE_HEADER.format(
            dataset=dataset_name,
            date=date_str,
            year=year,
        )
        # CSV header row
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header_row)
        yield buf.getvalue()

        # Stream rows
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(query)
                while True:
                    rows = cur.fetchmany(500)
                    if not rows:
                        break
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    for r in rows:
                        if row_transform:
                            r = row_transform(r)
                        w.writerow(r)
                    yield buf.getvalue()
        except Exception as e:
            yield f"# ERROR: {str(e)[:200]}\n"

    resp = Response(generate(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{dataset_name.lower().replace(" ", "-")}-{year}.csv"')
    resp.headers["Cache-Control"] = "public, max-age=3600, s-maxage=3600"
    resp.headers["X-DC-Attribution-Required"] = "yes"
    resp.headers["X-DC-License"] = "CC-BY-4.0"
    return resp


# ── Dataset registry ───────────────────────────────────────────────
_DATASETS = {
    "facilities": {
        "name": "Data Center Facilities",
        "description": "12,500+ tracked data center facilities with operator, location, capacity, status.",
        "query": """
            SELECT id, name, provider, city, state, country, latitude, longitude,
                   power_mw, status, source, first_seen
              FROM facilities
             WHERE name IS NOT NULL
             ORDER BY power_mw DESC NULLS LAST
             LIMIT 15000""",
        "header": ["id", "name", "provider", "city", "state", "country",
                   "latitude", "longitude", "power_mw", "status",
                   "source", "first_seen"],
    },
    "dcpi-markets": {
        "name": "DCPI Market Scores",
        "description": "276 data center markets with BUILD/CAUTION/AVOID verdict + 4 numeric scores.",
        "query": """
            SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, iso, state, verdict,
                   excess_power_score, constraint_score,
                   time_to_power_months, queue_wait_months, computed_at
              FROM market_power_scores
             WHERE market_slug IS NOT NULL
             ORDER BY market_slug, computed_at DESC""",
        "header": ["market_slug", "market_name", "iso", "state", "verdict",
                   "excess_power_score", "constraint_score",
                   "time_to_power_months", "queue_wait_months", "computed_at"],
    },
    "pipeline": {
        "name": "Capacity Pipeline",
        "description": "1,000+ data center pipeline projects with operator, market, MW, phase, status, completion date.",
        "query": """
            SELECT operator, market, capacity_mw, phase, status,
                   completion_date, notes
              FROM capacity_pipeline
             WHERE capacity_mw IS NOT NULL
             ORDER BY capacity_mw DESC NULLS LAST
             LIMIT 2000""",
        "header": ["operator", "market", "capacity_mw", "phase", "status",
                   "completion_date", "notes"],
    },
    "isos": {
        "name": "ISO Snapshot Stats",
        "description": "11 North American ISOs with facility footprint + DCPI rollup.",
        "query": """
            SELECT iso,
                   COUNT(DISTINCT market_slug) AS markets_scored,
                   COUNT(*) FILTER (WHERE verdict = 'BUILD') AS build_count,
                   COUNT(*) FILTER (WHERE verdict = 'CAUTION') AS caution_count,
                   COUNT(*) FILTER (WHERE verdict = 'AVOID') AS avoid_count,
                   ROUND(AVG(excess_power_score)::numeric, 1) AS avg_excess,
                   ROUND(AVG(constraint_score)::numeric, 1) AS avg_constraint,
                   ROUND(AVG(time_to_power_months)::numeric, 1) AS avg_ttp_months,
                   NOW() AS as_of
              FROM market_power_scores
             WHERE iso IS NOT NULL AND iso <> ''
             GROUP BY iso
             ORDER BY avg_excess DESC NULLS LAST""",
        "header": ["iso", "markets_scored", "build_count", "caution_count",
                   "avoid_count", "avg_excess", "avg_constraint",
                   "avg_ttp_months", "as_of"],
    },
}


@open_data_csv_bp.route("/api/v1/open-data/manifest.json", methods=["GET"])
def manifest():
    """List of available datasets."""
    out = {
        "license": "CC-BY-4.0",
        "attribution": "Required. Cite: \"Source: dchub.cloud\" or DOI-style citation in CSV header.",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "datasets": [
            {
                "slug": slug,
                "name": meta["name"],
                "description": meta["description"],
                "url": f"https://dchub.cloud/api/v1/open-data/{slug}.csv",
                "format": "csv",
                "license": "CC-BY-4.0",
            }
            for slug, meta in _DATASETS.items()
        ],
        "schema": {
            "facilities": "Per-facility metadata. Power = nameplate MW where reported.",
            "dcpi-markets": "DCPI verdict + 4 numeric scores per market. Latest computed_at only.",
            "pipeline": "In-construction + planned + announced projects with operator + market.",
            "isos": "Aggregate stats per ISO across all markets it covers.",
        },
        "links": {
            "landing_page": "https://dchub.cloud/open-data",
            "api_docs": "https://dchub.cloud/api-docs",
            "by_the_numbers": "https://dchub.cloud/by-the-numbers",
            "contact": "hello@dchub.cloud",
        },
    }
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200


@open_data_csv_bp.route("/api/v1/open-data/<slug>.csv", methods=["GET"])
def serve_csv(slug):
    """Stream a single dataset as CSV with attribution header.

    r42ab (2026-05-27): bulk downloads now require a free dev key. The
    citation/prose use-case (AI agents, journalist quotes, per-market
    drill) stays free; only the BULK CSV grab requires email signup.
    Rationale: 25 free dev keys claimed, 1 paid conversion — gating
    the bulk grab is the lowest-friction nudge that still leaves the
    CC-BY-4.0 citation moat intact."""
    from flask import request as _req
    api_key = (_req.headers.get('X-API-Key') or _req.args.get('api_key') or '').strip()
    if not api_key:
        return jsonify(
            ok=False,
            error="free_key_required",
            message=("Bulk CSV downloads require a free DC Hub dev key. "
                     "Per-market data, narratives, and citation use stay free."),
            claim_free_key="https://dchub.cloud/signup",
            attribution_required="DC Hub · CC-BY-4.0 · https://dchub.cloud",
            alternative="Cite specific markets free at https://dchub.cloud/dcpi/<slug>",
        ), 401, {"WWW-Authenticate": 'X-API-Key realm="DC Hub open data"'}

    meta = _DATASETS.get(slug)
    if not meta:
        return jsonify(
            ok=False,
            error="dataset_not_found",
            known=list(_DATASETS.keys()),
            manifest="https://dchub.cloud/api/v1/open-data/manifest.json",
        ), 404
    # Coerce date/timestamp columns to ISO strings in row transform
    def _row_xform(r):
        return [
            v.isoformat() if hasattr(v, "isoformat") else
            (str(v) if v is not None else "")
            for v in r
        ]
    return _stream_csv(meta["name"], meta["query"], meta["header"],
                       row_transform=_row_xform)
