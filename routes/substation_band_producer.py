"""LANE 1 producer: write facility['substation_band'].

Until this file existed, `util/thin_content._infra_rows()` read a key that
NOTHING in the repo wrote — no column, no migration, no backfill — so arming
THIN_INFRA_SLICE=1 rendered a band on exactly 0 pages. See
tests/test_thin_content_lanes.py, whose inert-lane guard is retired by this
change, and routes/thin_content_master_shell.py's lane1_infra block.

★ WHAT IS PUBLISHED IS A BAND, NOT AN ASSET. The reader renders one string
("within 5 km"). No substation name, voltage, count or coordinate ever reaches
the page — that read stays the paid product. The band is deliberately coarse
enough that it cannot be inverted into a location: "within 25 km" over a
~2,000 km² disc is a presence signal, not a siting answer.

★★★★ AND IT IS PUBLISHED ONLY WHERE IT WAS MEASURED. `substations` is US-only
(HIFLD) plus scattered OSM rows, so an empty search box abroad meant "we hold no
data for this country", not "far away" — yet the first cut banded it "over 25 km"
regardless, on 8,911 of 12,942 rows. Corrected TWICE: first by gating the top
band on coverage evidence in a 1.5° box, which still left 252 Canadian and
Mexican rows riding on US substations 46-184 km across the border; then by
taking the evidence from the SEARCH box alone. An empty search box now yields ''
and the page renders nothing. See _AUDIT_BOX_DEG for the audit, and
?sample=N on the status endpoint for the diagnostic that made it visible.

★★ NO QUERY AT RENDER TIME. `_infra_rows` performs no DB work by contract —
it reads a precomputed column. That is why this is a backfill writing a
column and not a helper the profile page calls: a per-render nearest-asset
query would put a pool hit on 12,942 public pages, and the pool already
saturates at 80 during job bursts.

★★★ DDL RUNS ONLY WHEN THE ADMIN ENDPOINT IS POSTED — never at boot. Boot-time
ALTERs on this service caused a deploy-failing statement-timeout storm
(2026-07-01: boot spanned ~8 min and the 5-min healthcheck window closed).
Same posture as routes/facility_slug_freeze.py.
"""

import os
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

substation_band_bp = Blueprint("substation_band", __name__)

# Both facility tables the profile page reads. discovered_facilities.id is a
# SERIAL (int); facilities.id is TEXT — every join below casts ::text on both
# sides so one statement serves both.
_FACILITY_TABLES = ("discovered_facilities", "facilities")

# ★ The bands. Ordered, coarse, and closed at the top. The final band is reached
# by MEASUREMENT — a nearest substation between 25 and 27.8 km — never by an
# empty box: "we looked and it is far" and "we never looked" are different facts
# and only the first is publishable. _infra_rows treats '' as absent, which is
# what a row with no coordinates, or no substation in the search box, gets.
_BANDS = ((1.0, "within 1 km"), (5.0, "within 5 km"),
          (10.0, "within 10 km"), (25.0, "within 25 km"))
_BAND_OVER = "over 25 km"

# ★★★★ 2026-08-16: THE EMPTY BOX DOES NOT PROVE DISTANCE. It proves distance
# ONLY where the substation dataset actually has rows. The original comment
# here read "an empty box proves '> 25 km'" — true under complete coverage,
# false in fact. `substations` is loaded from HIFLD Electric Substations
# (hifld_substation_loader.py:41; outFields NAME,CITY,STATE,COUNTY,…), a
# US-only feed, plus whatever OSM/learned rows infrastructure_discovery.py has
# added. So for a facility in Frankfurt the box came back empty because the
# dataset holds no German substations at all — and the producer published
# "over 25 km", a measured-sounding claim, on that basis.
#
# It was not a rounding error. Of 12,942 live facilities with coordinates,
# 8,911 (68.9%) banded "over 25 km", while the four real bands summed to 4,031
# — about the US share of an 18,121-facility global fleet. The shape gives it
# away independently: 2,645 rows "within 1 km" but only 29 in the 5–10 km ring
# is not a geographic distribution, it is a dense field (US) beside an empty
# one (everywhere else). Real data centres sit near transmission by definition;
# a genuine 69% beyond 25 km is not a thing.
#
# The first fix gated the top band on positive coverage evidence found in a
# 1.5° box. That was the right idea at the wrong radius, and 252 rows survived
# it — see _AUDIT_BOX_DEG for the audit that killed it. THE RULE NOW: an empty
# SEARCH box is not evidence of anything. km IS NULL → '' (absent), the page
# renders nothing, and every band that does render is a measurement.
#
# Deliberately NOT a country allowlist, and this is why the radius mattered:
# `substations` is not purely HIFLD — infrastructure_discovery.py also loads
# OSM substations, which are global — so hardcoding "US" would go stale the
# moment that feed lands a row abroad. Reading the data at the measuring
# radius keeps that property and widens by itself as coverage widens.
_NO_COVERAGE = ""

# Half-height of the search box in degrees of latitude. 0.25° ≈ 27.8 km, which
# strictly contains the 25 km band, so the query never has to scan the whole
# 79,686-row table.
_BOX_DEG = 0.25

# Half-height of the AUDIT box — 1.5° ≈ 167 km, up to ~236 km at the corners.
#
# ★★★★ 2026-08-16, SECOND CORRECTION: THIS IS NO LONGER A COVERAGE GATE.
# It was, and it was too wide to mean what it said. `substations` is HIFLD,
# US-only. A 167 km box drawn around a facility just north of the border
# reaches into the United States, finds American substations, and concludes
# "the dataset knows this area" — so the row kept "over 25 km" instead of
# blanking. Audited across all 252 survivors of the first correction:
#
#     coverage_km   min 46.0   median 73.5   max 183.7
#     25–40 km: 0 rows.   country: CA 239 · MX 11 · US 2 (both border-mislabelled)
#
# NOT ONE had a substation within 40 km. A real distance distribution piles up
# just past the 25 km cutoff; this one starts at 46. OVHcloud Beauharnois, 30 km
# from Montréal, published "over 25 km" on the strength of a substation 46 km
# away across the border in New York. Same false-precision class as the 8,911,
# one order of magnitude smaller and correspondingly harder to see.
#
# The error was treating coverage as a property of a BOX rather than of the area
# the facility is in. A substation 46 km away in another country is not evidence
# about this facility. The write path now takes its coverage evidence from the
# SEARCH box (below) and nothing else; this constant survives only to power the
# ?sample= audit, where "how far away does the dataset actually start" is the
# whole question. It must never re-enter the banding decision.
_AUDIT_BOX_DEG = 1.5


def _get_conn():
    from main import get_db
    return get_db()


def _table_exists(cur, table) -> bool:
    cur.execute("SELECT to_regclass(%s)", (table,))
    return cur.fetchone()[0] is not None


def _column_exists(cur, table, col) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, col))
    return cur.fetchone() is not None


def ensure_band_schema(conn):
    """Idempotent DDL: add substation_band to both facility tables.

    Safe to call on every admin hit. Deliberately NOT called on boot."""
    cur = conn.cursor()
    added = []
    for table in _FACILITY_TABLES:
        try:
            if not _table_exists(cur, table):
                continue  # table absent in this env — skip, don't fail
            if not _column_exists(cur, table, "substation_band"):
                # Short lock_timeout so a contended ALTER gives up fast instead
                # of sitting out the full statement_timeout (the boot-DDL-storm
                # lesson, applied even off the boot path).
                cur.execute("SET LOCAL lock_timeout = '2s'")
                cur.execute(
                    f"ALTER TABLE {table} ADD COLUMN substation_band TEXT")
                added.append(f"{table}.substation_band")
            # Partial index — only backfilled rows. Keeps it small; the read
            # path filters on "has a band", never on a specific band value.
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_substation_band "
                f"ON {table} (substation_band) "
                f"WHERE substation_band IS NOT NULL")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("band schema for %s: %s", table, e)
    return added


def band_for_km(km, in_coverage=True):
    """The banding decision, in Python. THE SQL CASE BELOW IS GENERATED FROM
    THE SAME _BANDS TUPLE, so this is the testable twin of what the backfill
    actually writes — not a second implementation that can drift from it.

    km   is the nearest substation distance found inside the SEARCH box, or
         None when the box was empty. A number is itself proof of coverage.
    in_coverage says whether any substation exists inside the wider COVERAGE
         box. It is consulted ONLY when km is None, because that is the only
         case where an empty result is ambiguous: "nothing near this facility"
         and "nothing in this dataset for this continent" look identical.

    Defaults to True so `band_for_km(3.2)` still reads naturally; the default
    is never taken on the None path in real use, where the caller always knows
    the coverage answer.
    """
    if km is not None:
        for edge, label in _BANDS:
            if km <= edge:
                return label
        return _BAND_OVER          # measured, and genuinely far
    # Nothing in the search box. Only claim distance if the dataset covers here.
    return _BAND_OVER if in_coverage else _NO_COVERAGE


def _band_case_sql(col: str, coverage_expr: str = "TRUE") -> str:
    """CASE mapping a km distance expression to a band label.

    Generated from _BANDS in order, so the SQL arms and band_for_km's loop
    cannot disagree: adding a band to the tuple changes both.

    `coverage_expr` is a boolean SQL expression standing in for band_for_km's
    in_coverage argument. It sits in the NULL arm only, which is what makes the
    wide probe cheap — Postgres never evaluates it for a row that matched in
    the tight box.
    """
    arms = " ".join(
        f"WHEN {col} <= {edge} THEN '{label}'" for edge, label in _BANDS)
    return (f"CASE WHEN {col} IS NULL THEN "
            f"(CASE WHEN {coverage_expr} THEN '{_BAND_OVER}' "
            f"ELSE '{_NO_COVERAGE}' END) "
            f"{arms} ELSE '{_BAND_OVER}' END")


def backfill_substation_bands(conn, table, batch=2000, max_batches=25,
                              force=False, repair=False):
    """Set substation_band for rows that do not have one yet.

    Returns (updated, remaining). One statement per batch, per-batch commit, so
    a timeout still leaves committed progress.

    ★ Distance is equirectangular, not haversine. Over a 27.8 km box the two
    differ by well under 0.1%, which cannot move a row across a band edge that
    is kilometres wide — and it stays a plain arithmetic expression the
    (lat, lng) index can drive, instead of a function call per candidate row.

    ★★ substations uses lat/lng, NOT latitude/longitude (facilities use the
    long names). Mixing them up silently yields an empty join and every row
    banding as 'over 25 km' — a wrong answer that looks like a working one.

    ★★★ repair=True re-bands ONLY the rows currently sitting in the top band.
    Those are the exact rows the pre-coverage-gate producer could have got
    wrong: a row reading "within 5 km" was written from a real measurement and
    is still correct, whereas "over 25 km" may mean either "measured, far" or
    "the dataset has never heard of this continent". Cheaper and far safer than
    force=True, which rewrites all 12,942 including the provably-good ones.
    """
    cur = conn.cursor()
    if not _table_exists(cur, table) or not _table_exists(cur, "substations"):
        return 0, 0
    if not _column_exists(cur, table, "substation_band"):
        return 0, 0

    # ★ repair is a RESET, not a third loop predicate. Re-banding in place on
    # `substation_band = 'over 25 km'` looks right and does not terminate: a row
    # that really is far gets rewritten to the same value, still matches the
    # predicate, and is picked again by every following batch — no progress, and
    # `remaining` never reaches zero. Clearing the suspect rows to NULL hands
    # them to the ordinary fill-the-NULLs path below, which provably terminates
    # because every outcome it writes (a band, the top band, or '') is non-NULL.
    if repair and not force:
        cur.execute(f"""
            UPDATE {table} SET substation_band = NULL
             WHERE substation_band = %s
               AND latitude IS NOT NULL AND longitude IS NOT NULL
        """, (_BAND_OVER,))
        conn.commit()

    # force=True re-bands every row with coordinates (use after a substations
    # reload); default only fills the gaps, so re-runs are cheap and idempotent.
    unset = "TRUE" if force else "t.substation_band IS NULL"
    unset_b = "TRUE" if force else "substation_band IS NULL"

    # ★★★★ THE COVERAGE EVIDENCE IS THE SEARCH BOX ITSELF, so this is FALSE.
    #
    # `nearest` already scanned the 0.25° box. If it came back NULL there is no
    # substation within 27.8 km, and the only question left is whether we may
    # convert that silence into "over 25 km". We may not: the wide probe that
    # used to answer yes was reading a neighbouring country's dataset (see
    # _AUDIT_BOX_DEG). Every band we publish is now a MEASUREMENT — km came
    # back non-NULL — and silence renders nothing.
    #
    # Written as the literal FALSE rather than a same-box EXISTS that would
    # provably always be false: a second scan of a box we just measured empty
    # is dead work, and a reader would have to prove it dead. The top band is
    # NOT retired — it is still reachable, from the ELSE arm, by a facility
    # whose nearest substation is measured between 25 and 27.8 km.
    coverage_sql = "FALSE"

    updated = 0
    for _ in range(max_batches):
        cur.execute(f"""
            WITH batch AS (
                SELECT id, latitude, longitude
                  FROM {table}
                 WHERE {unset_b}
                   AND latitude IS NOT NULL AND longitude IS NOT NULL
                 LIMIT %s
            ),
            nearest AS (
                SELECT b.id AS bid,
                       b.latitude AS blat, b.longitude AS blng,
                       MIN(111.32 * sqrt(
                             power(s.lat - b.latitude, 2)
                           + power((s.lng - b.longitude)
                                   * cos(radians(b.latitude)), 2))) AS km
                  FROM batch b
                  LEFT JOIN substations s
                    ON s.lat BETWEEN b.latitude - {_BOX_DEG}
                                 AND b.latitude + {_BOX_DEG}
                   AND s.lng BETWEEN b.longitude - ({_BOX_DEG} / GREATEST(
                                         cos(radians(b.latitude)), 0.01))
                                 AND b.longitude + ({_BOX_DEG} / GREATEST(
                                         cos(radians(b.latitude)), 0.01))
                 GROUP BY b.id, b.latitude, b.longitude
            )
            UPDATE {table} AS t
               SET substation_band = {_band_case_sql('n.km', coverage_sql)}
              FROM nearest n
             WHERE t.id::text = n.bid::text
               AND {unset}
        """, (batch,))
        n = cur.rowcount or 0
        conn.commit()
        updated += n
        if n < batch:
            break

    # Rows with no coordinates can never carry a band. Sentinel them to ''
    # (which _infra_rows reads as absent) so they stop being rescanned on every
    # run — the same '' posture canonical_slug uses for unslugables.
    try:
        cur.execute(f"""
            UPDATE {table} SET substation_band = ''
             WHERE substation_band IS NULL
               AND (latitude IS NULL OR longitude IS NULL)
        """)
        conn.commit()
    except Exception:
        conn.rollback()

    cur.execute(f"""
        SELECT COUNT(*) FROM {table}
         WHERE substation_band IS NULL
           AND latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    remaining = cur.fetchone()[0]
    return updated, remaining


def band_coverage(conn) -> dict:
    """How many LIVE pages actually carry a band — the number the lane1_infra
    board block reports as `renders_on_pages`.

    Uses the same fleet filter the profile page and the board use:
    COALESCE(is_duplicate, 0) = 0. Counting without it inflates the answer with
    rows that never render a page.
    """
    cur = conn.cursor()
    out = {"renders_on_pages": 0, "by_band": {}, "live_with_coords": 0}
    if not _table_exists(cur, "discovered_facilities"):
        return out
    if not _column_exists(cur, "discovered_facilities", "substation_band"):
        return out
    cur.execute("""
        SELECT COUNT(*) FROM discovered_facilities
         WHERE COALESCE(is_duplicate, 0) = 0
           AND latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    out["live_with_coords"] = cur.fetchone()[0]
    cur.execute("""
        SELECT substation_band, COUNT(*)
          FROM discovered_facilities
         WHERE COALESCE(is_duplicate, 0) = 0
           AND substation_band IS NOT NULL AND substation_band <> ''
         GROUP BY substation_band
    """)
    rows = cur.fetchall()
    out["by_band"] = {b: c for b, c in rows}
    out["renders_on_pages"] = sum(c for _, c in rows)

    # Split the '' sentinel by WHY it is blank. Both render nothing, but they
    # are different facts and the pricing decision needs them apart: "we have no
    # coordinates for this site" is a gap in our fleet data, while "the
    # substation dataset does not cover this country" is a gap in the feed —
    # and the second is the one that used to be published as "over 25 km".
    cur.execute("""
        SELECT COUNT(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL),
               COUNT(*) FILTER (WHERE latitude IS NOT NULL
                                  AND longitude IS NOT NULL)
          FROM discovered_facilities
         WHERE COALESCE(is_duplicate, 0) = 0
           AND substation_band = ''
    """)
    no_coords, no_coverage = cur.fetchone()
    out["blank_no_coords"] = int(no_coords or 0)
    out["blank_no_substation_coverage"] = int(no_coverage or 0)
    return out


def top_band_sample(conn, limit: int = 25) -> list:
    """The rows sitting in the top band, each with the distance to its nearest
    substation ANYWHERE IN THE COVERAGE BOX — not just the 25 km search box.

    ★ WHY THIS EXISTS. `over 25 km` is the one band written from an ABSENCE:
    the tight box came back empty and the wide coverage probe said "the dataset
    does know this area". Both halves are invisible in the aggregate, so the
    band cannot be audited from `by_band` counts — the pre-gate producer
    published it on 8,911 rows and the number looked ordinary. The honest
    reading of a row here is `coverage_km`: a facility whose nearest substation
    is 30 km away was MEASURED far, while one whose nearest is 150 km away is a
    facility on the ragged edge of a dataset that thins out around it, and the
    band is closer to a coverage statement than a distance.

    ★ Read-only, admin-gated, and bounded. The wide scan is affordable ONLY
    because the top band is small (252 rows at 2026-08-16); it is deliberately
    not exposed for the dense bands, where it would scan the whole table.
    """
    cur = conn.cursor()
    if not _table_exists(cur, "discovered_facilities"):
        return []
    if not _column_exists(cur, "discovered_facilities", "substation_band"):
        return []
    limit = max(1, min(int(limit), 300))
    cur.execute(f"""
        WITH top AS (
            SELECT id, name, country, latitude, longitude
              FROM discovered_facilities
             WHERE COALESCE(is_duplicate, 0) = 0
               AND substation_band = %s
               AND latitude IS NOT NULL AND longitude IS NOT NULL
             ORDER BY id
             LIMIT %s
        )
        SELECT t.id, t.name, t.country, t.latitude, t.longitude,
               MIN(111.32 * sqrt(
                     power(s.lat - t.latitude, 2)
                   + power((s.lng - t.longitude)
                           * cos(radians(t.latitude)), 2))) AS coverage_km
          FROM top t
          LEFT JOIN substations s
            ON s.lat BETWEEN t.latitude - {_AUDIT_BOX_DEG}
                         AND t.latitude + {_AUDIT_BOX_DEG}
           AND s.lng BETWEEN t.longitude - ({_AUDIT_BOX_DEG} / GREATEST(
                                 cos(radians(t.latitude)), 0.01))
                         AND t.longitude + ({_AUDIT_BOX_DEG} / GREATEST(
                                 cos(radians(t.latitude)), 0.01))
         GROUP BY t.id, t.name, t.country, t.latitude, t.longitude
         ORDER BY coverage_km DESC NULLS FIRST
    """, (_BAND_OVER, limit))
    out = []
    for fid, name, country, lat, lng, km in cur.fetchall() or []:
        out.append({
            "id": str(fid), "name": name, "country": country,
            "lat": float(lat), "lng": float(lng),
            # None can only mean the coverage probe found nothing — which the
            # gate should have caught. Surfaced, never silently coerced to a
            # number, because it would be a real defect and not a far site.
            "coverage_km": (round(float(km), 1) if km is not None else None),
        })
    return out


def _authorized() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY", "")
    return bool(expected) and request.headers.get("X-Admin-Key") == expected


@substation_band_bp.route('/api/v1/admin/facilities/substation-band/status',
                          methods=['GET'])
def substation_band_status():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    conn = _get_conn()
    try:
        out = {"success": True, **band_coverage(conn)}
        # ?sample=N audits the top band — the only band written from an absence.
        # Absent by default: the aggregate is the cheap call the board makes.
        n = request.args.get("sample")
        if n:
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = 25
            out["top_band_sample"] = top_band_sample(conn, n)
            out["top_band_sample_basis"] = (
                "coverage_km = distance to the nearest substation within "
                f"{_AUDIT_BOX_DEG}° (~167 km), i.e. the probe that let this "
                "row keep the top band instead of blanking to ''. A value just "
                "over 25 means MEASURED far; a large value means the dataset "
                "thins out around this facility and the band is closer to a "
                "coverage statement than a distance. null = coverage probe "
                "found nothing, which the gate should have blanked — a defect.")
        return jsonify(out)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@substation_band_bp.route('/api/v1/admin/facilities/substation-band/backfill',
                          methods=['POST'])
def substation_band_backfill():
    """★ Runs off the edge budget. Admin POSTs through Cloudflare abort at the
    ROUTE_TIMEOUTS budget while the origin keeps working, so keep max_batches
    modest and re-POST — progress is committed per batch."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    force = request.args.get("force") == "1"
    # repair=1: clear the suspect top band and re-derive it under the coverage
    # gate. Needed once after 2026-08-16 — rows written by the pre-gate producer
    # carry "over 25 km" for facilities the substation dataset never covered.
    repair = request.args.get("repair") == "1"
    batch = min(int(request.args.get("batch", 2000)), 5000)
    max_batches = min(int(request.args.get("max_batches", 25)), 100)
    conn = _get_conn()
    try:
        added = ensure_band_schema(conn)
        result = {}
        for table in _FACILITY_TABLES:
            updated, remaining = backfill_substation_bands(
                conn, table, batch=batch, max_batches=max_batches,
                force=force, repair=repair)
            result[table] = {"updated": updated, "remaining": remaining}
        return jsonify({"success": True, "schema_added": added,
                        "repair": repair, "force": force,
                        "tables": result, "coverage": band_coverage(conn)})
    except Exception as e:
        logger.warning("substation-band backfill failed: %s", e)
        return jsonify({"success": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
