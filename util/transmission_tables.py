"""
util/transmission_tables.py — 2026-07-29, shell(#41) transmission repair.

WHY THIS MODULE EXISTS
──────────────────────
The name "transmission_lines" meant TWO DIFFERENT TABLES depending on which
endpoint you asked, and the smaller one was the one wired into the product:

  transmission_lines       94,626 rows. THE MAINTAINED TABLE. Refreshed by
      routes/transmission_ingest.py (EIA US_Electric_Power_Transmission_Lines,
      full-replace in one transaction). ATTRIBUTES ONLY — the upstream service
      is queried with returnGeometry=false, so the table stores NO geometry.
      Live schema verified 2026-07-29 via /api/v1/admin/schema (14 columns:
      id, hifld_id, name, operator, voltage_kv, from_sub, to_sub, length_miles,
      state, status, line_type, source, last_updated, created_at). No lat, no
      lng, no geom. This one case is where LIVE == the repo DDL at
      land_power_crawler.py:139-155.

  transmission_lines_eia   56,108 rows. A GEOCODED SNAPSHOT — the only
      transmission table that carries lat/lng, and therefore the only one that
      can serve a spatial map layer. ★ IT HAS NO WRITER ANYWHERE IN THE REPO.
      Grep for INSERT/UPDATE/DELETE/COPY/TRUNCATE against it returns zero hits;
      every reference is a read or a docstring. It CANNOT be current by
      construction, and it has no timestamp column, so it cannot even report
      its own age.

CONSEQUENCE: the paid Land & Power map layer, the MCP `transmission` layer, and
per-site grid proximity counts are all served from the 56,108-row snapshot, so
38,518 maintained lines (40.7%) are invisible on every spatial surface.

WHY THIS IS NOT A ONE-LINE REPOINT: the maintained table has no coordinates, so
a spatial query cannot be pointed at it. Repointing would not return more lines
— it would raise UndefinedColumn, and every one of those call sites swallows
errors into an empty result or a 0, so the layer would silently go BLANK. That
is strictly worse than the shortfall.

SO THE HONEST FIX, which this module implements the vocabulary for:
  (a) publish 94,626 — bound live from the MAINTAINED table — as the layer's
      real line count, wherever a transmission count is served;
  (b) name the snapshot for what it is (GEOCODED_SNAPSHOT_KEY) and state its
      vintage wherever its rows are served, so one field name stops meaning two
      tables;
  (c) carry the geocoding gap as an explicit, machine-readable figure instead of
      a silent 41% shortfall.

The real repair — backfilling coordinates for all 94,626 maintained lines —
is a DATA capability that does not exist yet (the ingest deliberately requests
returnGeometry=false). `geocoding_gap()` is the standing, self-measuring record
of that gap. See GEOCODING_GAP_TRACKING.

HOUSE RULES HONORED HERE
  - Every figure binds live (COUNT(*) / MAX()) or is a floor that rounds DOWN.
  - UNMEASURED emits None plus a `reason`, NEVER 0. A count of 0 from this
    module always means "we counted zero rows", never "the query failed".
  - Fail soft: a failed probe yields None + reason, never a raise, so a caller
    can serve its rows without the figure rather than 500ing.

No DB work happens at import time.
"""
import logging
import math
import threading
import time

logger = logging.getLogger(__name__)

# ── Table identity ───────────────────────────────────────────────────────────
# The maintained, refreshed, attributes-only national set.
MAINTAINED_TABLE = "transmission_lines"
# The un-writable geocoded snapshot. Physical table name kept verbatim: this is
# what SQL must say. Do NOT rename the physical table without a migration —
# renaming here is a SERVED-FIELD rename, which is the part that was lying.
GEOCODED_SNAPSHOT_TABLE = "transmission_lines_eia"

# The name this snapshot is SERVED under from now on. The old served key
# `transmission_lines` is reclaimed for the maintained table, so a caller
# reading `transmission_lines` gets the 94,626 national count and a caller
# reading `transmission_lines_geocoded_snapshot` knows it is looking at the
# partial, undated, coordinate-bearing subset.
GEOCODED_SNAPSHOT_KEY = "transmission_lines_geocoded_snapshot"

MAINTAINED_KEY = "transmission_lines"

# Why the snapshot cannot self-report its age (12 live columns, none temporal).
SNAPSHOT_VINTAGE_UNKNOWN_REASON = (
    f"{GEOCODED_SNAPSHOT_TABLE} has no timestamp column (no created_at, "
    "updated_at or last_updated among its 12 live columns), so its vintage "
    "cannot be read from the table. It also has no writer in the repo, so it "
    "is a frozen snapshot of unknown date — treat it as older than "
    f"{MAINTAINED_TABLE}.")

# SERIALIZED — a caller (human or citing agent) needs this one line to state the
# shortfall honestly. Kept deliberately short: it ships on every map/MCP response.
GEOCODING_GAP_REASON = (
    f"{MAINTAINED_TABLE} stores no geometry, so only the frozen "
    f"{GEOCODED_SNAPSHOT_TABLE} subset can appear on spatial surfaces.")

# NOT SERIALIZED — the standing remediation record. This is engineering to-do
# text; putting it on every paid map response and every agent tool result is
# pure payload (it is why the disclosure block was 2,006 bytes). It lives here
# as the durable record of the gap, referenced from this module's docstring.
GEOCODING_GAP_TRACKING = (
    "OPEN DATA GAP — not a query bug. Closing it requires ingesting geometry "
    "(or centroids) for the maintained set: re-run the EIA transmission ingest "
    "with returnGeometry=true, derive a representative lat/lng per line, and "
    "add lat/lng columns to " + MAINTAINED_TABLE + ". Until then every spatial "
    "transmission figure is a FLOOR, never the national total.")

# ── Coverage memo ────────────────────────────────────────────────────────────
# COUNT(*) over 94K + 56K rows is a pair of seq scans. The map endpoint is the
# single most expensive surface on a 1-replica backend, so the coverage block is
# memoized process-locally and independently of any per-bbox payload cache
# (which is keyed by bbox and would re-scan for every distinct viewport).
_COVERAGE_TTL_SECONDS = 900
_coverage_memo = {"v": None, "t": 0.0}
_coverage_lock = threading.Lock()


def _scalar(cur, sql):
    """Run a scalar query. Returns (value, None) or (None, reason). Never raises.

    A DB error is rolled back so it cannot poison the caller's transaction —
    the BUG-021 failure mode (dchub-frontend/admin-qa.html:68), where one failed
    query aborted every later query on the same connection.
    """
    try:
        cur.execute(sql)
        row = cur.fetchone()
        if not row or row[0] is None:
            return None, f"query returned no value: {sql.strip()}"
        return row[0], None
    except Exception as e:                                    # noqa: BLE001
        try:
            cur.connection.rollback()
        except Exception:                                     # noqa: BLE001
            pass
        return None, f"{type(e).__name__}: {str(e)[:160]}"


def maintained_count(cur):
    """Live COUNT(*) of the MAINTAINED table. -> (int|None, reason|None)."""
    return _scalar(cur, f"SELECT COUNT(*) FROM {MAINTAINED_TABLE}")


def geocoded_count(cur):
    """Live COUNT(*) of geocoded rows actually usable for a spatial layer.

    Counts only rows with BOTH coordinates present, which is what every spatial
    consumer filters on — so this is the true serving ceiling, not the raw
    table size.
    """
    return _scalar(
        cur,
        f"SELECT COUNT(*) FROM {GEOCODED_SNAPSHOT_TABLE} "
        "WHERE lat IS NOT NULL AND lng IS NOT NULL")


def maintained_vintage(cur):
    """MAX(last_updated) of the MAINTAINED table as ISO-8601.

    -> (iso_str|None, reason|None).
    """
    val, reason = _scalar(cur, f"SELECT MAX(last_updated) FROM {MAINTAINED_TABLE}")
    if val is None:
        return None, reason
    return (val.isoformat() if hasattr(val, "isoformat") else str(val)), None


def geocoding_gap(maintained, geocoded):
    """The explicit, machine-readable shortfall. Never invents a number.

    Returns None when either input is unmeasured — an unmeasured gap must not
    render as a confident 0. `pct_of_maintained_absent` is floored to one
    decimal so the published shortfall can never overstate reality.
    """
    if maintained is None or geocoded is None:
        return None
    if maintained <= 0:
        return None
    absent = maintained - geocoded
    if absent <= 0:
        # Snapshot is not behind (or is somehow larger — a data surprise worth
        # reporting rather than a negative gap).
        return {
            "lines_without_coordinates": 0 if absent == 0 else None,
            "pct_of_maintained_absent": 0.0 if absent == 0 else None,
            "reason": None if absent == 0 else (
                "geocoded snapshot reports MORE rows than the maintained "
                "table; the two sets have diverged and the gap is undefined"),
        }
    return {
        "lines_without_coordinates": absent,
        # floor to 1dp — published shortfall rounds DOWN, never up
        "pct_of_maintained_absent": math.floor(absent * 1000.0 / maintained) / 10.0,
        "reason": GEOCODING_GAP_REASON,
        # remediation text intentionally NOT serialized — see GEOCODING_GAP_TRACKING
    }


def coverage(cur, use_cache=True):
    """The coverage/vintage/gap block to splice into any transmission payload.

    Every figure is bound live. Anything unmeasured is None with a sibling
    `*_basis` or `reason` string. Never raises; never emits 0 for "unknown".
    """
    if use_cache:
        hit = _coverage_memo["v"]
        if hit is not None and (time.time() - _coverage_memo["t"]) <= _COVERAGE_TTL_SECONDS:
            return hit

    m_count, m_reason = maintained_count(cur)
    g_count, g_reason = geocoded_count(cur)
    m_vintage, v_reason = maintained_vintage(cur)

    # PAYLOAD DISCIPLINE: this block ships on every response from a
    # high-volume paid map layer and from the agent-facing MCP layer. Measured
    # 2026-07-29, the long-prose version was 2,006 bytes — 45% overhead on a
    # 27-row result. So: every FIGURE keeps its basis (house rule), but the
    # explanation is stated ONCE in a single `basis` string plus a short `note`,
    # not repeated per field. The long-form rationale lives in this module's
    # docstring and in `reason`/`tracking` on the gap, which is the one place a
    # reader actually needs it.
    block = {
        "layer": "electric_transmission",

        # What the ROWS in this response actually came from.
        "served_from_key": GEOCODED_SNAPSHOT_KEY,
        "served_from_table": GEOCODED_SNAPSHOT_TABLE,
        "geocoded_lines_available": g_count,
        "geocoded_snapshot_last_updated": None,
        "geocoded_snapshot_has_writer": False,

        # The MAINTAINED national set — the honest denominator.
        "maintained_key": MAINTAINED_KEY,
        "maintained_table": MAINTAINED_TABLE,
        "maintained_lines_total": m_count,
        "maintained_last_updated": m_vintage,
        "maintained_stores_geometry": False,

        "geocoding_gap": geocoding_gap(m_count, g_count),
        "is_complete": (
            None if (m_count is None or g_count is None) else g_count >= m_count),

        # One basis string for every figure above (house rule: no figure without
        # its basis) — not one long string per field.
        "basis": (
            "live COUNT(*) per table (geocoded counted WHERE lat/lng NOT NULL); "
            f"maintained_last_updated = live MAX(last_updated) FROM "
            f"{MAINTAINED_TABLE}. geocoded_snapshot_last_updated is null "
            "because that table has no timestamp column and no writer — its "
            "age is unknowable from the data, and it predates the maintained set."),
        "note": (
            "Line counts here are a FLOOR: rows come from a frozen geocoded "
            "snapshot, the maintained table is larger. See geocoding_gap."),
    }
    # Only surface unmeasured reasons when something actually failed — an
    # all-null reason set is pure payload on the happy path.
    reasons = {k: v for k, v in (
        ("geocoded_lines_available", g_reason),
        ("maintained_lines_total", m_reason),
        ("maintained_last_updated", v_reason),
    ) if v}
    if reasons:
        block["unmeasured_reasons"] = reasons

    if use_cache:
        with _coverage_lock:
            _coverage_memo["v"] = block
            _coverage_memo["t"] = time.time()
    return block


def reset_coverage_cache():
    """Drop the memo. For tests and admin refresh paths."""
    with _coverage_lock:
        _coverage_memo["v"] = None
        _coverage_memo["t"] = 0.0
