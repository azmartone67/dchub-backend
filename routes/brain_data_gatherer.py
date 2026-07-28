"""
routes/brain_data_gatherer.py — Brain GATHER-step Source 7: facility breakdowns.

The brain's self-resolving "unverified facilities" loop ("21,762 tracked vs
~2,300 verified") is answerable from a SINGLE table, `discovered_facilities` —
the same table canonical_stats already treats as authoritative. This module
hands the GATHER step REAL, measured breakdowns (total verified/tracked, by
country, by operator, by US ISO) so the REASON + REFUTE steps cite ground-truth
numbers instead of looping on a vague gap.

DESIGN (mirrors routes/brain_investigator.py Sources 1-6):
  · exposes gather_facility_breakdowns() -> list[dict] in the GATHER evidence
    shape: {"claim": str, "source": str, "value": int|float|str}.
  · BEST-EFFORT: every query is wrapped; any failure (slow / missing table /
    missing column) degrades to "breakdown unavailable" or is skipped. The
    function NEVER raises and NEVER blocks the synchronous ~48s investigation.
  · FLAG-GATED: BRAIN_DATA_GATHER_ENABLED (default OFF — ships dark).
  · READ-ONLY: SELECT only. No writes. No model-generated SQL — every query is
    PRE-WRITTEN and parameterless (params=None, so no psycopg2 empty-tuple-%
    trap). A short per-connection statement_timeout caps any slow scan.
  · SCHEMA-CORRECT: column names confirmed at runtime via information_schema
    before any breakdown query runs; an absent column degrades gracefully.

CANONICAL DEFINITIONS (match canonical_stats.py get_canonical_stats):
  VERIFIED := COALESCE(is_duplicate,0)=0        -- the FLEET filter
  TRACKED  := COUNT(*)
2026-07-16 metric-truth fix: dropped `AND merged_at IS NULL` from VERIFIED —
same bug canonical_stats fixed on 2026-07-10 (issue #1539). The merge pipeline
stamps merged_at on EVERY promoted fleet row, so the old combined predicate
counted the drained pending queue (reads 0) and every breakdown reported
"verified 0 / tracked 21,992" while canonical stats said ~4,958 verified.
Live now: tracked 21,993 / verified 4,958 / unverified 17,035. Uses
`discovered_facilities` ONLY — never joins the curated `facilities` table
(conflating them is exactly the double-count this avoids).

Real column names (discovered_facilities, verified via information_schema):
  country (text, clean 2-letter ISO; "US" only US variant; 265 blank),
  provider (text, operator/owner; no column literally named operator/owner),
  state (text), is_duplicate (integer 0/1), merged_at (text).
There is NO ISO/region column — ISO is derived from `state` via the canonical
map inlined below (mirrors routes/dcpi.py:809 _state_to_iso).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# ── env / flag ───────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """Source 7 ships DARK. Default OFF until an operator flips the flag.
    Even though the queries are proven fast (<31ms warm) and read-only, the
    gate keeps the GATHER step's evidence surface under operator control."""
    return _truthy(os.environ.get("BRAIN_DATA_GATHER_ENABLED"))


# Cap any single breakdown scan. seq scans on a 21.8K-row table run in
# ~26-31ms warm; 4s is a generous ceiling that still trips long before the
# investigation's ~48s budget is at risk.
_STMT_TIMEOUT_MS = 4000


# ── DB (raw psycopg2; mirrors brain_investigator._conn) ──────────────
def _conn():
    """Raw psycopg2 connection. Returns None (never raises) on any failure so
    the caller degrades to 'source unavailable'."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_data_gatherer: _conn failed: %s", e)
    return None


def _columns_present(cur, table: str, wanted: set) -> set:
    """Return the subset of `wanted` columns that ACTUALLY exist on `table`
    (the repo DDL lies — verify the live schema). Best-effort: {} on error."""
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s",
            (table,),
        )
        live = {r[0] for r in cur.fetchall()}
        return {c for c in wanted if c in live}
    except Exception:
        return set()


def _fmt_int(n) -> str:
    """1859 -> '1,859'. Best-effort; passes non-ints through as str."""
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


# Canonical US state -> ISO map, inlined as a VALUES join so the breakdown
# stays consistent with the rest of the platform. discovered_facilities has
# NO ISO column.
#
# r-iso-taxonomy (2026-07-28): GENERATED from util/iso_taxonomy.STATE_ISO
# instead of hand-maintained. It was the fourth hand-copied version of this
# map and had drifted to the same wrong values as routes/dcpi.py
# (NC→PJM, MO→MISO, MI→PJM, SC→SOCO), so the brain's ISO breakdown was
# attributing Carolinas and Kansas City facilities to RTOs that do not
# serve them. Generating it makes drift structurally impossible.
#
# NOTE: this is a STATE-level rollup, so it cannot express the per-market
# overrides (Kansas City is Evergy/SPP while Missouri defaults to MISO).
# That is acceptable for a coarse facility breakdown; anything needing
# per-market truth must go through iso_taxonomy.resolve_iso(slug, state).
def _build_state_iso_values() -> str:
    from util.iso_taxonomy import STATE_ISO
    pairs = ",".join(f"('{st}','{iso}')" for st, iso in sorted(STATE_ISO.items()))
    return f"(VALUES\n  {pairs}\n) AS m(st, iso)"


_STATE_ISO_VALUES = _build_state_iso_values()


# ── PRE-WRITTEN, schema-verified, read-only breakdown queries ────────
# All are parameterless seq scans on discovered_facilities (21.8K rows). Each
# spec declares the columns it depends on so the runner can skip it if a column
# is absent (schema drift) rather than erroring.

_TOTAL_SQL = """
SELECT COUNT(*) AS tracked,
       COUNT(*) FILTER (WHERE COALESCE(is_duplicate,0)=0) AS verified,
       COUNT(*) FILTER (WHERE NOT (COALESCE(is_duplicate,0)=0)) AS unverified
FROM discovered_facilities
"""

_COUNTRY_SQL = """
SELECT COALESCE(NULLIF(TRIM(country),''),'(unknown)') AS country,
       COUNT(*) AS tracked,
       COUNT(*) FILTER (WHERE COALESCE(is_duplicate,0)=0) AS verified
FROM discovered_facilities
GROUP BY 1 ORDER BY tracked DESC LIMIT 5
"""

_OPERATOR_SQL = """
SELECT COALESCE(NULLIF(TRIM(provider),''),'(unknown)') AS operator,
       COUNT(*) AS tracked,
       COUNT(*) FILTER (WHERE COALESCE(is_duplicate,0)=0) AS verified
FROM discovered_facilities
GROUP BY 1 ORDER BY tracked DESC LIMIT 5
"""

_US_ISO_SQL = """
SELECT COALESCE(m.iso,'(other/unmapped)') AS iso,
       COUNT(*) AS tracked,
       COUNT(*) FILTER (WHERE COALESCE(d.is_duplicate,0)=0) AS verified
FROM discovered_facilities d
LEFT JOIN {state_iso} ON UPPER(TRIM(d.state)) = m.st
WHERE d.country = 'US'
GROUP BY 1 ORDER BY tracked DESC LIMIT 5
""".format(state_iso=_STATE_ISO_VALUES)


def gather_facility_breakdowns() -> list[dict]:
    """Source 7: facility breakdowns from `discovered_facilities`.

    Returns a compact list of evidence items in the brain GATHER-step shape:
        {"claim": str, "source": str, "value": int|float|str}

    Items emitted (top-N, never full dumps):
      · total verified vs tracked vs unverified
      · unverified/tracked by country (top 5)
      · unverified/tracked by operator/provider (top 5)
      · tracked/verified by US ISO (top 5)

    BEST-EFFORT: returns [] when disabled or unreachable; a single failing
    breakdown degrades to nothing (or a 'breakdown unavailable' note) without
    aborting the others. NEVER raises."""
    if not _enabled():
        return []

    conn = _conn()
    if conn is None:
        return []

    items: list[dict] = []
    try:
        # Autocommit so one failed breakdown can't poison a shared tx; each
        # query is independent and read-only.
        try:
            conn.autocommit = True
        except Exception:
            pass

        with conn.cursor() as cur:
            # Cap any slow scan; trips long before the ~48s investigation budget.
            try:
                cur.execute("SET statement_timeout = %s", (_STMT_TIMEOUT_MS,))
            except Exception:
                pass

            # Verify the live schema once (the repo DDL lies). Skip any
            # breakdown whose required column is absent.
            present = _columns_present(
                cur, "discovered_facilities",
                {"country", "provider", "state", "is_duplicate"},
            )
            # Core verified/tracked definition needs the dup flag; without it
            # NO breakdown is meaningful, so bail cleanly. (merged_at is NOT
            # part of the verified predicate — issue #1539.)
            if "is_duplicate" not in present:
                return []

            # (1) TOTAL verified vs tracked.
            try:
                cur.execute(_TOTAL_SQL)
                row = cur.fetchone()
                if row:
                    tracked, verified, unverified = (
                        int(row[0] or 0), int(row[1] or 0), int(row[2] or 0),
                    )
                    items.append({
                        "claim": "facilities verified vs tracked vs unverified (canonical)",
                        "source": "discovered_facilities (verified = COALESCE(is_duplicate,0)=0, the fleet filter)",
                        "value": (f"verified {_fmt_int(verified)} / "
                                  f"tracked {_fmt_int(tracked)} / "
                                  f"unverified {_fmt_int(unverified)}"),
                    })
            except Exception as e:
                logger.warning("brain_data_gatherer: total breakdown failed: %s", e)

            # (2) BY COUNTRY (top 5) — needs country.
            if "country" in present:
                try:
                    cur.execute(_COUNTRY_SQL)
                    rows = cur.fetchall()
                    if rows:
                        parts = [f"{r[0]} {_fmt_int(r[1])} tracked / "
                                 f"{_fmt_int(r[2])} verified" for r in rows]
                        items.append({
                            "claim": "facilities by country, tracked vs verified (top 5)",
                            "source": "discovered_facilities GROUP BY country",
                            "value": "; ".join(parts),
                        })
                except Exception as e:
                    logger.warning("brain_data_gatherer: country breakdown failed: %s", e)

            # (3) BY OPERATOR/provider (top 5) — needs provider.
            if "provider" in present:
                try:
                    cur.execute(_OPERATOR_SQL)
                    rows = cur.fetchall()
                    if rows:
                        parts = [f"{r[0]} {_fmt_int(r[1])} tracked / "
                                 f"{_fmt_int(r[2])} verified" for r in rows]
                        items.append({
                            "claim": "facilities by operator/provider, tracked vs verified (top 5; "
                                     "note Equinix vs 'Equinix, Inc.' not normalized)",
                            "source": "discovered_facilities GROUP BY provider",
                            "value": "; ".join(parts),
                        })
                except Exception as e:
                    logger.warning("brain_data_gatherer: operator breakdown failed: %s", e)

            # (4) BY US ISO (top 5) — needs state + country.
            if {"state", "country"} <= present:
                try:
                    cur.execute(_US_ISO_SQL)
                    rows = cur.fetchall()
                    if rows:
                        parts = [f"{r[0]} {_fmt_int(r[1])} tracked / "
                                 f"{_fmt_int(r[2])} verified" for r in rows]
                        items.append({
                            "claim": "US facilities by ISO, tracked vs verified (top 5; "
                                     "ISO derived from state via canonical map)",
                            "source": "discovered_facilities WHERE country='US' GROUP BY state->ISO",
                            "value": "; ".join(parts),
                        })
                except Exception as e:
                    logger.warning("brain_data_gatherer: US-ISO breakdown failed: %s", e)

        return items
    except Exception as e:
        logger.warning("brain_data_gatherer: gather_facility_breakdowns failed: %s", e)
        return items  # whatever we accumulated; never raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
