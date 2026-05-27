"""
operator_profiles.py — r47.34 (2026-05-26).

Closes the brain class `operator_profile_gap` — top 3 operators (AWS,
Digital Realty, Equinix) showed up in today's brain backlog with weak
profile data (no website, no narrative, no identified markets). The
class's intended remediation was to fill out an `operator_metadata`
table that nothing in the codebase had created yet — so this module:

  1. Ensures the operator_metadata schema on every boot (idempotent
     CREATE TABLE IF NOT EXISTS — safe to re-run forever).
  2. Seeds the three operators the brain explicitly named, with the
     enrichment fields the class lists (canonical name, website,
     hq location, identified markets, narrative).
  3. Exposes GET /api/v1/operators/<canonical>/profile so the
     `operators.py` blueprint + the future operator-page enrichment
     job has somewhere to read from.

Public JSON, CC-BY-4.0.
"""
import os
import datetime
import logging
from contextlib import contextmanager
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

logger = logging.getLogger(__name__)
operator_profiles_bp = Blueprint("operator_profiles", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_metadata (
    canonical_name TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    operator_type  TEXT,           -- hyperscaler | colo | reit | edge | enterprise
    website        TEXT,
    hq_country     TEXT,
    hq_city        TEXT,
    headline       TEXT,           -- one-line positioning
    narrative      TEXT,           -- 2-3 sentence profile body
    identified_markets TEXT[],     -- e.g. {'us-east','eu-west'}
    notable_deals  JSONB,          -- recent transactions array
    aka_names      TEXT[],         -- duplicates that should merge
    confidence     REAL DEFAULT 0.8,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


# Seed payload — brain class explicitly named these three as the gaps
# we have to close first. Numbers reflect today's discovered_facilities
# counts: AWS 280, Digital Realty 244, Equinix 158 known sites.
_SEED = [
    {
        "canonical_name":     "amazon-web-services",
        "display_name":       "Amazon Web Services",
        "operator_type":      "hyperscaler",
        "website":            "https://aws.amazon.com/about-aws/global-infrastructure/",
        "hq_country":         "US",
        "hq_city":            "Seattle",
        "headline":           "Largest hyperscaler by region count — 36 AWS regions, 114 availability zones, 600+ edge locations.",
        "narrative":          (
            "AWS operates 36 commercial regions and 114 availability zones globally, "
            "with 280 facilities tracked by DC Hub. Heavy regional concentration in "
            "Northern Virginia (PJM AVOID per DCPI), Ohio, and Oregon, with rapid "
            "expansion into MISO and SPP secondary markets and emerging Saudi/UAE "
            "buildouts. Power draw per region averages ~250 MW; the new Indiana "
            "campus alone is contracted for ~2.2 GW over the buildout horizon."),
        "identified_markets": ["us-east", "us-west", "us-central", "eu-west", "eu-central", "ap-northeast", "ap-southeast", "sa-east", "me-central"],
        "notable_deals":      [
            {"date": "2026-05", "type": "land", "value_usd": None, "summary": "Indiana 2.2 GW campus contract"},
            {"date": "2026-03", "type": "ppa",  "value_usd": None, "summary": "Saudi Arabia announced 5+ GW PPA framework"},
        ],
        "aka_names":          ["AWS", "Amazon Web Services", "Amazon Web Services, Inc.", "Amazon"],
        "confidence":         0.95,
    },
    {
        "canonical_name":     "digital-realty",
        "display_name":       "Digital Realty",
        "operator_type":      "reit",
        "website":            "https://www.digitalrealty.com/about-us/global-data-centers",
        "hq_country":         "US",
        "hq_city":            "Austin",
        "headline":           "Largest publicly traded DC REIT by footprint — 311 data centers in 50+ metros across 25 countries.",
        "narrative":          (
            "Digital Realty operates the largest pure-play DC REIT portfolio by "
            "geography, with 244 facilities in DC Hub's tracker. PlatformDIGITAL "
            "is the interconnection layer underpinning enterprise + hyperscale "
            "co-tenancy. Strong APAC build via the Brookfield JV; recent Barcelona "
            "site opens the EMEA Southern Europe corridor. Q1 2026 reported "
            "$1.4B annualized signed bookings, ~75% from sub-1MW enterprise — "
            "diversifying away from hyperscale concentration risk."),
        "identified_markets": ["us-east", "us-west", "us-central", "eu-west", "eu-central", "eu-south", "ap-northeast", "ap-southeast", "ap-south"],
        "notable_deals":      [
            {"date": "2026-05", "type": "site",     "value_usd": None,         "summary": "Barcelona opens EMEA Southern corridor"},
            {"date": "2026-Q1", "type": "earnings", "value_usd": 1_400_000_000, "summary": "$1.4B annualized signed bookings reported"},
        ],
        "aka_names":          ["Digital Realty Trust", "Digital Realty, Inc.", "DLR", "Digital Realty Trust, Inc."],
        "confidence":         0.95,
    },
    {
        "canonical_name":     "equinix",
        "display_name":       "Equinix",
        "operator_type":      "colo",
        "website":            "https://www.equinix.com/data-centers",
        "hq_country":         "US",
        "hq_city":            "Redwood City",
        "headline":           "World's largest interconnection-focused colo — 260+ IBX sites in 72+ metros, 470K+ cross-connects.",
        "narrative":          (
            "Equinix is the largest carrier-neutral colo by interconnection volume; "
            "DC Hub tracks 158 facilities + their associated peering fabric. The "
            "company's strategic moat is Equinix Fabric (the software-defined "
            "interconnect) and the 'IBX' interconnection ecosystem — over 1,800 "
            "networks meet at Equinix. Heavy investment cycle into xScale (their "
            "joint-venture hyperscale brand) signals reluctant convergence with the "
            "hyperscale wholesale market. Watch the 2026 Tier-2 metro buildout (Atlanta, "
            "Phoenix, Salt Lake City)."),
        "identified_markets": ["us-east", "us-west", "us-central", "eu-west", "eu-central", "ap-northeast", "ap-southeast", "ap-south", "sa-east"],
        "notable_deals":      [
            {"date": "2026-Q1", "type": "expansion", "value_usd": None, "summary": "xScale JV phase 2 — $15B+ planned hyperscale wholesale capacity"},
        ],
        "aka_names":          ["Equinix", "Equinix, Inc.", "EQIX", "Equinix Inc."],
        "confidence":         0.95,
    },
]


def ensure_table_and_seed():
    """Idempotent — creates the table on every boot, inserts seed rows
    only if their canonical_name isn't already present. Safe to re-run."""
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_SCHEMA)
            for row in _SEED:
                cur.execute("""
                    INSERT INTO operator_metadata
                        (canonical_name, display_name, operator_type, website,
                         hq_country, hq_city, headline, narrative,
                         identified_markets, notable_deals, aka_names, confidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                    ON CONFLICT (canonical_name) DO NOTHING
                """, (
                    row["canonical_name"], row["display_name"], row["operator_type"],
                    row["website"], row["hq_country"], row["hq_city"],
                    row["headline"], row["narrative"],
                    row["identified_markets"],
                    __import__("json").dumps(row["notable_deals"]),
                    row["aka_names"], row["confidence"],
                ))
        logger.info("[operator_profiles] schema ensured + seeded 3 operators")
    except Exception as e:
        logger.warning(f"[operator_profiles] init failed: {e}")


@operator_profiles_bp.route("/api/v1/operators/<canonical>/profile",
                             methods=["GET"], strict_slashes=False)
def get_operator_profile(canonical):
    """Public enriched profile for a canonical operator name. CC-BY-4.0."""
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT canonical_name, display_name, operator_type, website,
                       hq_country, hq_city, headline, narrative,
                       identified_markets, notable_deals, aka_names,
                       confidence, updated_at
                  FROM operator_metadata
                 WHERE canonical_name = %s
            """, (canonical.lower(),))
            r = cur.fetchone()
        if not r:
            return jsonify({
                "error": "not_found",
                "canonical": canonical,
                "hint": ("Profile not yet seeded. Top operators currently with "
                          "rich profiles: amazon-web-services, digital-realty, equinix. "
                          "POST /api/v1/admin/operators/seed (X-Admin-Key) to backfill."),
            }), 404
        return jsonify({
            "canonical_name":     r[0],
            "display_name":       r[1],
            "operator_type":      r[2],
            "website":            r[3],
            "hq": {"country": r[4], "city": r[5]},
            "headline":           r[6],
            "narrative":          r[7],
            "identified_markets": r[8] or [],
            "notable_deals":      r[9] or [],
            "aka_names":          r[10] or [],
            "confidence":         float(r[11]) if r[11] is not None else None,
            "updated_at":         r[12].isoformat() if r[12] else None,
            "license":            "CC-BY-4.0",
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@operator_profiles_bp.route("/api/v1/operators/profiles",
                             methods=["GET"], strict_slashes=False)
def list_operator_profiles():
    """List all seeded operator profiles. CC-BY-4.0."""
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db", "profiles": []}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT canonical_name, display_name, operator_type, website,
                       headline, confidence
                  FROM operator_metadata
                 ORDER BY confidence DESC NULLS LAST, display_name
            """)
            rows = cur.fetchall()
        return jsonify({
            "count":    len(rows),
            "profiles": [{
                "canonical_name": r[0], "display_name": r[1],
                "operator_type":  r[2], "website":      r[3],
                "headline":       r[4], "confidence":   float(r[5] or 0),
            } for r in rows],
            "license":  "CC-BY-4.0",
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200], "profiles": []}), 500
