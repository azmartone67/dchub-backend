"""Provenance envelope v1 (2026-07-11, LOCKED 2026-07-11) — the
citation-confidence moat.

WHY: AI agents citing DC Hub need to know HOW MUCH to trust each figure.
Nobody else in the vertical (LandGate/WoodMac, DC Byte, datacenterHawk,
Enverus) stamps per-record verification + collection-level provenance on
their data responses — this envelope is the asset that survives an
incumbent MCP launch.

V1 CONTRACT LOCK (Gemini partnership contract-hardening, 2026-07-11):
``provenance_version``, ``fallback_url`` and ``default_v`` are now part of
the frozen v1 schema. Changing the MEANING of any existing key requires a
version bump; adding keys stays allowed (additive-only).

DESIGN (payload-discipline locked — a past optimization cut list payloads
−47%; do not undo it):

  * ONE collection-level ``provenance`` block per response (never per
    record)::

        {
          "provenance_version": 1,                  # v1 lock — always present
          "source":  "...where the data comes from...",
          "method":  "...how it was collected/derived...",
          "as_of":   "2026-07-11T...",              # only when meaningful
          "verification_counts": {"verified": N, "tracked": N},  # optional
          "cite_url_template": "https://dchub.cloud/facilities/{slug}",
          "fallback_url": "https://dchub.cloud/facilities/directory",
                          # always present — the deterministic cite URL a
                          # model uses when a record lacks the template's
                          # substitution variable
          "default_v": "tracked",                   # always present — the
                          # confidence tier a record WITHOUT a per-record
                          # ``v`` field inherits (the LOWEST tier that can
                          # legitimately appear in the collection)
          "license": "CC-BY-4.0",
          "cite_as": "DC Hub, dchub.cloud"
        }

  * Per-record: ONE compact field only — ``v``:
      - facilities:  "verified" | "tracked"   (canonical fleet filter
        COALESCE(is_duplicate,0)=0 on discovered_facilities — see
        canonical_stats.py, issue #1539)
      - queue/large-load: "published" | "inferred"  (published ISO figure
        vs name-match/derived inference — mirrors the depth shell's
        published_queue/inferred split)
  * Per-record ``as_of`` only where it genuinely differs per row (deals
    have dates, grid rows have mix_period — those already exist; never
    duplicate them).
  * cite_url_template lives at COLLECTION level — never per-row URLs.

FAIL-SOFT CONTRACT: nothing in this module may ever break a response.
Every public helper catches everything and degrades to a minimal-but-valid
value. Existing _source/_cite/citation fields stay untouched (additive
only, backward compatible).
"""
from __future__ import annotations

import os
import threading
import time

PROVENANCE_VERSION = 1
LICENSE = "CC-BY-4.0"
CITE_AS = "DC Hub, dchub.cloud"

# ★2026-08-10 — CC-BY-4.0 IS NOT TRUE OF EVERY LAYER, and stamping it on the
# facility corpus was an over-claim we had live. That corpus is a COMPOSITE:
# ~7,844 of its discovery rows come from OpenStreetMap, which is ODbL 1.0 —
# share-alike. ODbL does not permit re-licensing derived data as CC-BY-4.0,
# so `license: "CC-BY-4.0"` on /api/v1/facilities asserted a grant we do not
# hold. PeeringDB rows separately require attribution we were not emitting.
#
# What IS ours to grant CC-BY-4.0: work DC Hub COMPUTED — DCPI scores and
# verdicts, the methodology, our normalised grid analysis. Those are derived
# analytical outputs reproducible from public inputs, and we want them cited.
#
# The layer is derived from the collection's cite template (the same signal
# `_fallback_for` already trusts), so no wiring site changes and no route has
# to remember which licence it is under. See DATA-LICENSE.md for the
# authoritative per-layer statement.
LICENSE_COMPOSITE = "Mixed — see https://dchub.cloud/data-sources"

# ODbL 1.0 §4.3 and PeeringDB both require attribution. Emitting it in the
# provenance block is how that obligation is actually discharged for an API
# consumer — a page nobody fetches does not attribute anything.
ATTRIBUTION_COMPOSITE = (
    "Contains data from OpenStreetMap contributors (ODbL 1.0, "
    "opendatacommons.org/licenses/odbl/1-0) and PeeringDB, plus operator "
    "disclosure and DC Hub curation. Full source list: "
    "https://dchub.cloud/data-sources"
)

# Canonical cite-URL templates (collection-level only — bytes discipline).
# These are literal TEMPLATES handed to agents (the {placeholder} is filled
# client-side per record), NOT hand-rolled URL emission — so they are built
# by concatenation to keep the url_registry chokepoint lint
# (tests/test_url_registry_chokepoint.py) authoritative for real emitters.
_BASE = "https://dchub.cloud"
FACILITY_CITE_TEMPLATE = _BASE + "/facilities/" + "{slug}"
MARKET_CITE_TEMPLATE = _BASE + "/markets/" + "{market_slug}"
DCPI_CITE_TEMPLATE = _BASE + "/dcpi/" + "{market_slug}"

# v1 fallback URLs — the deterministic page a model cites when a record
# lacks the cite_url_template's substitution variable. Most specific stable
# page per surface; same concatenation style as the templates above (keeps
# the url_registry chokepoint lint authoritative for real emitters).
FACILITIES_FALLBACK_URL = _BASE + "/facilities/" + "directory"
MARKETS_FALLBACK_URL = _BASE + "/markets/" + "directory"
DEFAULT_FALLBACK_URL = _BASE

_MINIMAL_BLOCK = {
    "provenance_version": PROVENANCE_VERSION,
    "source": "DC Hub (dchub.cloud)",
    "fallback_url": DEFAULT_FALLBACK_URL,
    "license": LICENSE,
    "cite_as": CITE_AS,
}


def _is_composite_layer(cite_template):
    """True when this collection is the composite facility corpus (upstream
    ODbL/attribution obligations apply). Derived from the cite template, the
    same signal `_fallback_for` uses. NEVER raises."""
    try:
        return "/facilities/" in str(cite_template or "")
    except Exception:
        return False


def _fallback_for(cite_template):
    """Map a collection cite template to its surface's fallback URL.
    facilities → the facilities directory; markets/DCPI → the markets
    directory; anything else (or no template) → the site root. NEVER
    raises."""
    try:
        t = str(cite_template or "")
        if "/facilities/" in t:
            return FACILITIES_FALLBACK_URL
        if "/markets/" in t or "/dcpi/" in t:
            return MARKETS_FALLBACK_URL
    except Exception:
        pass
    return DEFAULT_FALLBACK_URL


def _iso(dt):
    """Best-effort ISO-8601 string from datetime/date/str. None on failure."""
    if dt is None:
        return None
    try:
        if hasattr(dt, "isoformat"):
            return dt.isoformat()
        s = str(dt).strip()
        return s or None
    except Exception:
        return None


def provenance_block(source, method, as_of=None, counts=None,
                     cite_template=None, fallback_url=None, *, default_v):
    """Build the collection-level provenance block. NEVER raises once
    called (``default_v`` is a REQUIRED keyword — forgetting it at a wiring
    site fails loudly at review/test time, by design).

    source        — where the data comes from (dataset/feed name).
    method        — how it was collected/derived, incl. what the per-record
                    ``v`` flag means on this response (when present).
    as_of         — datetime/date/ISO-string data vintage; omit for live
                    row-level-dated collections (deals, grid mix_period).
    counts        — optional {"verified": N, "tracked": N} (or
                    {"published": N, ...}) verification tally.
    cite_template — optional collection-level cite URL template. ★It also
                    selects the LICENCE: a facilities template marks the
                    composite corpus, which carries upstream ODbL/attribution
                    obligations and therefore `LICENSE_COMPOSITE` plus an
                    `attribution` field, NOT the CC-BY-4.0 we grant over our
                    own computed work. See DATA-LICENSE.md.
    fallback_url  — optional explicit fallback cite URL; when omitted it is
                    derived from cite_template (facilities → facilities
                    directory, markets/dcpi → markets directory, else the
                    site root). Always emitted (v1 lock).
    default_v     — REQUIRED (v1 lock): the confidence tier a record
                    WITHOUT a per-record ``v`` field inherits. Pass the
                    LOWEST (most conservative) tier that can legitimately
                    appear in this collection — facilities: "tracked";
                    queue/grid: "published" or "inferred"; deals: "tracked".
    """
    try:
        _composite = _is_composite_layer(cite_template)
        block = {
            "provenance_version": PROVENANCE_VERSION,
            "source": str(source),
            "method": str(method),
            "default_v": str(default_v),
            "license": LICENSE_COMPOSITE if _composite else LICENSE,
            "cite_as": CITE_AS,
        }
        if _composite:
            block["attribution"] = ATTRIBUTION_COMPOSITE
        a = _iso(as_of)
        if a:
            block["as_of"] = a
        if counts:
            try:
                vc = {str(k): int(v) for k, v in dict(counts).items()
                      if v is not None}
                if vc:
                    block["verification_counts"] = vc
            except Exception:
                pass
        if cite_template:
            block["cite_url_template"] = str(cite_template)
        # v1 lock: fallback_url is ALWAYS present (explicit > derived).
        try:
            block["fallback_url"] = (str(fallback_url) if fallback_url
                                     else _fallback_for(cite_template))
        except Exception:
            block["fallback_url"] = DEFAULT_FALLBACK_URL
        return block
    except Exception:
        blk = dict(_MINIMAL_BLOCK)
        try:
            blk["default_v"] = str(default_v)
        except Exception:
            pass
        return blk


def attach_provenance(payload, source, method, as_of=None, counts=None,
                      cite_template=None, fallback_url=None, *, default_v):
    """Stamp ``payload['provenance']`` in place (dict payloads only; never
    overwrites an existing block; never raises once called — ``default_v``
    is a required keyword, mirroring provenance_block). Returns the
    payload."""
    try:
        if isinstance(payload, dict) and "provenance" not in payload:
            payload["provenance"] = provenance_block(
                source, method, as_of=as_of, counts=counts,
                cite_template=cite_template, fallback_url=fallback_url,
                default_v=default_v)
    except Exception:
        pass
    return payload


def verified_flag(row, default="tracked"):
    """Per-record facilities flag: 'verified' | 'tracked'.

    'verified' = the row passes the canonical fleet filter
    COALESCE(is_duplicate,0)=0 (discovered_facilities). Rows that do not
    carry an ``is_duplicate`` key (e.g. the legacy ``facilities`` table)
    fall back to the conservative default — floors never over-claim.
    NEVER raises."""
    try:
        if isinstance(row, dict) and "is_duplicate" in row:
            return "tracked" if (row.get("is_duplicate") or 0) else "verified"
    except Exception:
        pass
    return default


def queue_flag(published=True):
    """Per-record queue/large-load flag: 'published' | 'inferred'.

    'published' = the ISO's own published queue/disclosure figure;
    'inferred'  = DC Hub derivation (name-match, fuel_type=Load inference,
    modeled estimate). NEVER raises."""
    try:
        return "published" if published else "inferred"
    except Exception:
        return "inferred"


def facility_verification_counts():
    """{'verified': N, 'tracked': N} from the cached canonical stats
    (canonical_stats.get_canonical_stats — 10-min TTL, floor-safe).
    Returns None on any failure so callers can omit the field."""
    try:
        from canonical_stats import get_canonical_stats
        s = get_canonical_stats()
        v = s.get("facilities_verified")
        t = s.get("facilities")
        if v and t:
            return {"verified": int(v), "tracked": int(t)}
    except Exception:
        pass
    return None


# ─── which POPULATION a verification_counts block describes ─────────────────
#
# ★2026-08-28 — `verification_counts` on /api/v1/facilities described a
# DIFFERENT table than the rows beside it. The authenticated arm
# (_list_facilities_full) serves `SELECT * FROM facilities WHERE
# duplicate_of_id IS NULL`, but stamped the counts from
# canonical_stats — which counts `discovered_facilities`. Measured live on
# 2026-08-28: the GB slice returned 834 rows out of the `facilities` table
# next to "verified 19,332 of tracked 27,099", and every one of those rows
# carried v="tracked", because `facilities` has no is_duplicate column at
# all. One response, two populations, presented as one.
#
# That is a PUBLISHED number, not an internal one: the MCP server tells
# agents to cite it as "N analyst-verified of M tracked facilities".
#
# Two rules come out of this, and they are why the legacy block below
# publishes `tracked` ALONE:
#
#  1. Count over the table you SERVE. A count of another table is not a
#     conservative approximation of this one — it is a different fact.
#  2. Do NOT invent a `verified` tier for a table that has none. The
#     obvious "fix" — calling the 22,130 rows that pass `duplicate_of_id
#     IS NULL` the verified subset — publishes a BIGGER over-claim than
#     the bug did, and against this repo's standing rule (main.py ~21578,
#     on the same ambiguity at /api/v1/stats): both dedup predicates "are
#     DE-DUPLICATION states, not source verifications, and neither should
#     be published as 'verified'". A row that survived cross-source
#     de-duplication has not been analyst-verified.
#
# So the counts and the per-record `v` flags now agree on this surface:
# every served row is "tracked", and the block says tracked-only.
#
# Both sentences below are quoted into `method` at the wiring sites so the
# population is stated in the response rather than inferred from the table
# name in `source`. tests/test_facilities_counts_population.py pins each
# call site to the pair matching the table it actually queries.

COUNTS_BASIS_DISCOVERED = (
    "verification_counts describe discovered_facilities — the same corpus "
    "these rows are drawn from (tracked = every row; verified = distinct "
    "canonical_slug passing the fleet filter COALESCE(is_duplicate,0)=0)"
)

COUNTS_BASIS_LEGACY = (
    "verification_counts describe the curated `facilities` table these rows "
    "are drawn from (tracked = rows passing the cross-source de-duplication "
    "filter duplicate_of_id IS NULL). That table carries no fleet-verification "
    "flag, so NO verified count is published for it and every record here is "
    "v='tracked'; do not compare this tracked figure with the "
    "discovered_facilities counts published by the free tier and the "
    "per-facility endpoints — they are different populations"
)

# 10-minute TTL, matching canonical_stats — these move slowly, and a hot
# list endpoint must not pay a COUNT(*) per request.
_LEGACY_TTL_S = 600
# This query MUST name `facilities`. It exists solely to count the table that
# `_list_facilities_full` actually serves; pointing it at `discovered_facilities`
# would restore the exact defect this module was changed to fix — counts that
# describe a different population than the rows beside them.
# (Token must sit within 2 lines of the match; the scanner only looks that far.)
# lint: legacy-facilities-ok
_LEGACY_COUNT_SQL = "SELECT COUNT(*) FROM facilities WHERE duplicate_of_id IS NULL"
_legacy_cache = None          # None = never measured; {} = measured-and-failed
_legacy_cache_ts = 0.0
_legacy_lock = threading.Lock()


def legacy_facility_counts():
    """{'tracked': N} counted over the `facilities` table AS SERVED by
    _list_facilities_full — i.e. behind the same `duplicate_of_id IS NULL`
    filter the rows come through.

    Deliberately has NO 'verified' key: `facilities` carries no
    is_duplicate column, so it has no verification tier to count, and
    de-duplication is not verification (see the note above). An agent
    reading this block can say how large the served population is and
    cannot say any of it was analyst-verified — which is the true state.

    Returns None on any failure so the caller omits the field: an omitted
    count beats a wrong one. NEVER raises."""
    global _legacy_cache, _legacy_cache_ts
    now = time.time()
    with _legacy_lock:
        if _legacy_cache is not None and (now - _legacy_cache_ts) < _LEGACY_TTL_S:
            return dict(_legacy_cache) or None
    measured = {}
    conn = None
    try:
        db = (os.environ.get("DATABASE_URL")
              or os.environ.get("NEON_DATABASE_URL"))
        if db:
            import psycopg2
            conn = psycopg2.connect(db, sslmode="require", connect_timeout=4)
            cur = conn.cursor()
            cur.execute(_LEGACY_COUNT_SQL)
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                measured = {"tracked": n}
    except Exception:
        measured = {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    # Cache the FAILURE too ({}), so a down DB costs one connect attempt per
    # TTL rather than one per request on a hot list endpoint.
    with _legacy_lock:
        _legacy_cache = measured
        _legacy_cache_ts = now
    return dict(measured) or None


# ---------------------------------------------------------------------------
# r-nullisland (2026-08-31): coordinates that are absent must SAY they are
# absent.
#
# Reported by a user on 2026-08-30 who had spent 2,563 API calls evaluating DC
# Hub for a comparison site and left because "the values did not appear to be
# accurate or reliable". Probing his complaint turned up
# /api/v1/facility/vantage-data-centers-ashburn-ii-va-united-states-54315501
# returning latitude 0.0, longitude 0.0 — while power_mw on the SAME row
# correctly returned null.
#
# 0,0 is Null Island: open water in the Gulf of Guinea. No data center is there.
# The value is an ingestion artifact of the `float(x or 0)` idiom (see
# hifld_neon_routes.py, site_risk_apis.py, data_grabber.py), which turns a
# missing coordinate into a confident, plottable, WRONG one.
#
# This is worse than a gap. A null tells a consumer to go find the number
# elsewhere; a 0.0 tells them they already have it. It is the exact opposite of
# what the rest of this module exists to do, and it silently spends the
# credibility the constraint_coverage/as_of work earned.
#
# ONLY (0,0) TOGETHER is treated as absent. lat=0 with a real longitude is a
# genuine equatorial location (Uganda, Ecuador, Indonesia, Kenya) and must be
# left completely alone.
# ---------------------------------------------------------------------------

_COORD_EPS = 1e-9

COORDS_KNOWN = "known"
COORDS_UNKNOWN = "unknown"

_NULL_ISLAND_REASON = (
    "stored as 0,0 (Null Island) — an ingestion placeholder for a missing "
    "coordinate, not a location. Normalised to null so it is not plotted or "
    "used for distance math."
)


def normalize_coordinates(row, lat_key="latitude", lon_key="longitude"):
    """Rewrite a Null-Island coordinate pair to nulls IN PLACE and stamp
    `coordinates_status` so the absence is explicit rather than implied.

    Returns the same dict for convenient chaining. Never raises — a coordinate
    field that is a string, a Decimal, or missing entirely all degrade to
    'unknown' rather than blowing up a facility response.
    """
    if not isinstance(row, dict):
        return row
    if lat_key not in row and lon_key not in row:
        return row

    def _num(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lat, lon = _num(row.get(lat_key)), _num(row.get(lon_key))

    if lat is None or lon is None:
        row[lat_key] = None if lat is None else lat
        row[lon_key] = None if lon is None else lon
        row["coordinates_status"] = COORDS_UNKNOWN
        return row

    if abs(lat) < _COORD_EPS and abs(lon) < _COORD_EPS:
        row[lat_key] = None
        row[lon_key] = None
        row["coordinates_status"] = COORDS_UNKNOWN
        row["coordinates_note"] = _NULL_ISLAND_REASON
        return row

    row["coordinates_status"] = COORDS_KNOWN
    return row
