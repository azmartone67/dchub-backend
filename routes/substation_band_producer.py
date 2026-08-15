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

# ★ The bands. Ordered, coarse, and closed at the top: anything with no
# substation inside the search box lands in the final band rather than NULL,
# so "we looked and it is far" is distinguishable from "we never looked"
# (NULL). _infra_rows treats '' as absent, which is what a row with no
# coordinates gets — it is not eligible for a band at all.
_BANDS = ((1.0, "within 1 km"), (5.0, "within 5 km"),
          (10.0, "within 10 km"), (25.0, "within 25 km"))
_BAND_OVER = "over 25 km"

# Half-height of the search box in degrees of latitude. 0.25° ≈ 27.8 km, which
# strictly contains the 25 km band, so an empty box proves "> 25 km" and the
# query never has to scan the whole 79,686-row table.
_BOX_DEG = 0.25


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


def band_for_km(km):
    """The banding decision, in Python. THE SQL CASE BELOW IS GENERATED FROM
    THE SAME _BANDS TUPLE, so this is the testable twin of what the backfill
    actually writes — not a second implementation that can drift from it.

    km is None when no substation fell inside the search box, which means
    "further than the box", i.e. the top band.
    """
    if km is None:
        return _BAND_OVER
    for edge, label in _BANDS:
        if km <= edge:
            return label
    return _BAND_OVER


def _band_case_sql(col: str) -> str:
    """CASE mapping a km distance expression to a band label.

    Generated from _BANDS in order, so the SQL arms and band_for_km's loop
    cannot disagree: adding a band to the tuple changes both.
    """
    arms = " ".join(
        f"WHEN {col} <= {edge} THEN '{label}'" for edge, label in _BANDS)
    return f"CASE WHEN {col} IS NULL THEN '{_BAND_OVER}' {arms} " \
           f"ELSE '{_BAND_OVER}' END"


def backfill_substation_bands(conn, table, batch=2000, max_batches=25,
                              force=False):
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
    """
    cur = conn.cursor()
    if not _table_exists(cur, table) or not _table_exists(cur, "substations"):
        return 0, 0
    if not _column_exists(cur, table, "substation_band"):
        return 0, 0

    # force=True re-bands every row with coordinates (use after a substations
    # reload); default only fills the gaps, so re-runs are cheap and idempotent.
    unset = "TRUE" if force else "t.substation_band IS NULL"
    unset_b = "TRUE" if force else "substation_band IS NULL"

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
                 GROUP BY b.id
            )
            UPDATE {table} AS t
               SET substation_band = {_band_case_sql('n.km')}
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
        return jsonify({"success": True, **band_coverage(conn)})
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
    batch = min(int(request.args.get("batch", 2000)), 5000)
    max_batches = min(int(request.args.get("max_batches", 25)), 100)
    conn = _get_conn()
    try:
        added = ensure_band_schema(conn)
        result = {}
        for table in _FACILITY_TABLES:
            updated, remaining = backfill_substation_bands(
                conn, table, batch=batch, max_batches=max_batches, force=force)
            result[table] = {"updated": updated, "remaining": remaining}
        return jsonify({"success": True, "schema_added": added,
                        "tables": result, "coverage": band_coverage(conn)})
    except Exception as e:
        logger.warning("substation-band backfill failed: %s", e)
        return jsonify({"success": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
