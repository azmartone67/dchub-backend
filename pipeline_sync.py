"""
DC Hub Pipeline → Neon Sync
Pulls latest pipeline data from DC Hub API, inserts new facilities into Neon,
and logs everything for review.

Replit Secrets required:
  NEON_DATABASE_URL  — Postgres connection string from Neon dashboard
  DCHUB_API_KEY      — DC Hub API key (optional, works without for free tier)

Run manually or via Replit's scheduled deployments (cron).
"""

import os
import json
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime

import psycopg2
from psycopg2.extras import execute_values

# ── Config ──────────────────────────────────────────────────────────────────

NEON_URL = os.environ["NEON_DATABASE_URL"]
DCHUB_API_KEY = os.environ.get("DCHUB_API_KEY", "")
DCHUB_BASE = os.environ.get("DCHUB_API_BASE", "http://127.0.0.1:5000")  # calls local DC Hub backend
TODAY = date.today().isoformat()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"pipeline_sync_{TODAY}.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Grid / connectivity mapping ─────────────────────────────────────────────

GRID_MAP = {
    "TX": "ERCOT",
    "VA": "PJM",
    "OH": "PJM",
    "NJ": "PJM",
    "MD": "PJM",
    "IL": "PJM",
    "TN": "MISO",
    "LA": "MISO",
    "WI": "MISO",
    "IN": "MISO",
    "MS": "MISO",
    "AR": "MISO",
    "MO": "SPP",
    "AZ": "WECC",
    "NM": "WECC",
    "OR": "BPA",
    "CA": "CAISO",
    "GA": "Southern",
    "SC": "Duke",
    "CO": "WAPA",
}

# ── City centroid coordinates ───────────────────────────────────────────────

COORDS = {
    "Memphis": (35.1495, -90.0490),
    "Abilene": (32.4487, -99.7331),
    "West Memphis": (35.1465, -90.1846),
    "Mount Pleasant": (42.7139, -87.8712),
    "Port Washington": (43.3872, -87.8712),
    "Hillsboro": (45.5229, -122.9898),
    "Singapore": (1.3521, 103.8198),
    "Dallas": (32.7767, -96.7970),
    "Columbus": (39.9612, -82.9988),
    "Ashburn": (39.0438, -77.4874),
    "Richmond": (37.5407, -77.4360),
    "Denver": (39.7392, -104.9903),
    "Atlanta": (33.7490, -84.3880),
    "El Paso": (31.7619, -106.4850),
    "Kansas City": (39.0997, -94.5786),
    "Phoenix": (33.4484, -112.0740),
    "Chicago": (41.8781, -87.6298),
    "Tokyo": (35.6762, 139.6503),
    "Meridian": (32.3643, -88.7037),
    "Richland Parish": (32.4179, -91.7137),
    "Albany": (32.7234, -99.2976),  # Shackelford County, TX
    "Helsinki": (60.1699, 24.9384),
    "Kuala Lumpur": (3.1390, 101.6869),
    "Sydney": (-33.8688, 151.2093),
}

# ── Status mapping ──────────────────────────────────────────────────────────

STATUS_MAP = {
    "operational": "Operational",
    "construction": "Under Construction",
    "announced": "Planned",
}

# ── Confidence by status ────────────────────────────────────────────────────

CONFIDENCE_MAP = {
    "operational": 0.85,
    "construction": 0.80,
    "announced": 0.70,
}

# ── Region mapping ──────────────────────────────────────────────────────────

REGION_MAP = {
    "US": "North America",
    "SG": "Asia Pacific",
    "JP": "Asia Pacific",
    "AU": "Asia Pacific",
    "MY": "Asia Pacific",
    "IE": "Europe",
    "FI": "Europe",
}


def fetch_pipeline():
    """Pull pipeline data directly from Neon capacity_pipeline table."""
    conn = psycopg2.connect(NEON_URL, sslmode="require")
    cur = conn.cursor()
    cur.execute("""
        SELECT id, operator, market, region, capacity_mw, phase, status,
               source, source_url, notes, confidence_score
        FROM capacity_pipeline
        ORDER BY capacity_mw DESC NULLS LAST
    """)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    cur.execute("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(capacity_mw), 0) AS total_mw
        FROM capacity_pipeline
    """)
    sr = cur.fetchone()
    stats = {"project_count": sr[0], "total_mw": float(sr[1])}
    cur.close()
    conn.close()
    projects = []
    for row in rows:
        r = dict(zip(cols, row))
        projects.append({
            "company": r.get("operator", "Unknown"),
            "project": f"{r.get('operator', 'Unknown')} {r.get('market', 'Unknown')}",
            "_db_id": r.get("id"),
            "market": r.get("market", "Unknown"),
            "capacity_mw": r.get("capacity_mw"),
            "status": (r.get("status") or "announced").lower(),
            "delivery": r.get("phase") or "TBD",
            "type": "wholesale",
            "preleased": False,
            "investment": 0,
            "_source_url": r.get("source_url", ""),
            "_notes": r.get("notes", ""),
            "_confidence": r.get("confidence_score"),
        })
    log.info(f"Fetched {len(projects)} pipeline projects from Neon capacity_pipeline")
    return projects, stats


def slugify(text):
    """Create a slug-style ID from text."""
    text = str(text)
    return (
        text.lower()
        .replace(" ", "-")
        .replace("(", "")
        .replace(")", "")
        .replace("/", "-")
        .replace("$", "")
        .replace("+", "plus")
        .replace(",", "")
        .replace(".", "")
        .replace("'", "")
        .strip("-")
    )


def parse_market(market):
    if not market:
        return None, None, "US"
    """Extract city, state, country from market string."""
    city, state, country = None, None, "US"

    # International markets
    intl = {
        "Singapore": (("Singapore", None, "SG")),
        "Tokyo": (("Tokyo", None, "JP")),
        "Japan": ((None, None, "JP")),
        "Ireland": ((None, None, "IE")),
        "Helsinki": (("Helsinki", None, "FI")),
        "Kuala Lumpur": (("Kuala Lumpur", None, "MY")),
        "Sydney": (("Sydney", None, "AU")),
        "Europe": ((None, None, None)),
    }

    for key, vals in intl.items():
        if key.lower() in market.lower():
            return vals

    # US: "City, ST" pattern
    if "," in market:
        parts = [p.strip() for p in market.split(",")]
        city = parts[0]
        state = parts[1] if len(parts) > 1 and len(parts[1]) == 2 else None
    else:
        # State-only or region
        state_names = {
            "Indiana": "IN", "Ohio": "OH", "Virginia": "VA",
            "New Jersey": "NJ", "Texas": "TX", "New Mexico": "NM",
            "South Carolina": "SC", "Maryland": "MD",
            "Northern Virginia": "VA", "N. Virginia": "VA",
            "Southern California": "CA", "Midwest": None,
            "United States": None,
        }
        state = state_names.get(market)

    return city, state, country


def build_facility(project):
    """Convert a DC Hub pipeline project into a facilities row dict."""
    city, state, country = parse_market(project["market"])

    # Determine region
    region = REGION_MAP.get(country, "North America") if country else "Europe"

    # Coordinates
    lat, lon = None, None
    if city and city in COORDS:
        lat, lon = COORDS[city]

    # Connectivity
    connectivity = GRID_MAP.get(state) if state else None

    # Status
    status = STATUS_MAP.get(project["status"], "Planned")

    # Confidence
    confidence = CONFIDENCE_MAP.get(project["status"], 0.65)
    if project.get("delivery") == "TBD":
        confidence = max(confidence - 0.10, 0.60)

    # Slug ID
    provider_slug = slugify(project["company"])
    name_slug = slugify(project["project"])
    facility_id = f"{provider_slug}-{name_slug}"
    # Truncate if too long
    if len(facility_id) > 60:
        facility_id = facility_id[:60].rstrip("-")

    # Source ID
    source_id = f"dchub_{name_slug}"

    # Raw data JSON
    raw_data = json.dumps({
        "use_case": project.get("type", "unknown"),
        "investment_m": project.get("investment", 0),
        "delivery": project.get("delivery", "TBD"),
        "preleased": project.get("preleased", False),
        "type": project.get("type", "unknown"),
    })

    return {
        "id": facility_id,
        "name": project["project"],
        "provider": project["company"],
        "address": project["market"],
        "city": city,
        "state": state,
        "country": country,
        "region": region,
        "latitude": lat,
        "longitude": lon,
        "power_mw": project.get("capacity_mw") or project.get("capacity_mw") or project.get("capacity"),
        "sqft": None,
        "status": status,
        "tier": None,
        "certifications": None,
        "connectivity": connectivity,
        "source": "dchub_pipeline",
        "source_url": "https://dchub.cloud",
        "source_id": source_id,
        "confidence": confidence,
        "first_seen": TODAY,
        "last_updated": TODAY,
        "raw_data": raw_data,
    }


def sync_to_neon(facilities):
    """Insert new facilities into Neon, skip duplicates, return counts."""
    inserted = []
    skipped = []

    conn = psycopg2.connect(NEON_URL, sslmode="require")
    cur = conn.cursor()

    for f in facilities:
        # Check for existing by id OR source_id
        cur.execute(
            "SELECT id FROM facilities WHERE id = %s OR source_id = %s",
            (f["id"], f["source_id"]),
        )
        if cur.fetchone():
            skipped.append(f["id"])
            continue

        cur.execute(
            """INSERT INTO facilities
               (id, name, provider, address, city, state, country, region,
                latitude, longitude, power_mw, sqft, status, tier,
                certifications, connectivity, source, source_url, source_id,
                confidence, first_seen, last_updated, raw_data)
            VALUES
               (%(id) ON CONFLICT DO NOTHINGs, %(name)s, %(provider)s, %(address)s, %(city)s,
                %(state)s, %(country)s, %(region)s, %(latitude)s,
                %(longitude)s, %(power_mw)s, %(sqft)s, %(status)s,
                %(tier)s, %(certifications)s, %(connectivity)s,
                %(source)s, %(source_url)s, %(source_id)s,
                %(confidence)s, %(first_seen)s, %(last_updated)s,
                %(raw_data)s)""",
            f,
        )
        inserted.append(f["id"])

    conn.commit()
    cur.close()
    conn.close()

    return inserted, skipped


def write_log(inserted, skipped, stats):
    """Write a human-readable sync log."""
    report = [
        f"# Pipeline Sync Report — {TODAY}",
        "",
        f"**Pipeline total:** {stats.get('total_mw', 'N/A')} MW across "
        f"{stats.get('unique_markets', 'N/A')} markets",
        f"**Projects fetched:** {len(inserted) + len(skipped)}",
        f"**New facilities inserted:** {len(inserted)}",
        f"**Skipped (already exist):** {len(skipped)}",
        "",
    ]

    if inserted:
        report.append("## New Inserts")
        for fid in inserted:
            report.append(f"- `{fid}`")
        report.append("")

    if skipped:
        report.append("## Skipped (duplicate)")
        for fid in skipped:
            report.append(f"- `{fid}`")
        report.append("")

    report_text = "\n".join(report)
    report_file = f"pipeline_sync_{TODAY}.md"

    with open(report_file, "w") as f:
        f.write(report_text)

    log.info(f"Report saved to {report_file}")
    return report_text


def main():
    log.info("=" * 60)
    log.info(f"Pipeline Sync starting — {datetime.now().isoformat()}")
    log.info("=" * 60)

    # 1. Fetch pipeline
    try:
        projects, stats = fetch_pipeline()
    except Exception as e:
        log.error(f"Failed to fetch DC Hub pipeline: {e}")
        raise

    # 2. Build facility records
    facilities = [build_facility(p) for p in projects]
    log.info(f"Built {len(facilities)} facility records")

    # 3. Sync to Neon
    try:
        inserted, skipped = sync_to_neon(facilities)
        log.info(f"Inserted: {len(inserted)} | Skipped: {len(skipped)}")
    except Exception as e:
        log.error(f"Neon sync failed: {e}")
        raise

    # 4. Write log
    report = write_log(inserted, skipped, stats)
    print(report)

    log.info("Pipeline Sync complete")


if __name__ == "__main__":
    main()
