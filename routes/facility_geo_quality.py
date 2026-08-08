"""facility_geo_quality.py — country-label correction from coordinates (2026-07-20).

Surfaced while de-duplicating: ~2,400 facilities carry coordinates that fall
outside their tagged `country` — a bulk ingestion stamped `country='US'` on a
lot of non-US data (French / German / Indian / Japanese / Canadian sites). The
COORDINATES are right; the country label is wrong. So `country=FR` misses real
French sites and `country=US` is polluted — and it fragments dedup (a site
mislabeled US can't collapse against its correctly-tagged twin).

FIX — coordinate-driven, conservative, reversible:
  * Infer the country from lat/lon via bounding boxes. Only a SINGLE unambiguous
    box match counts; points in overlapping boxes (borders) or no box are left
    for review, never auto-changed.
  * City guard: if the row's city is a known city of the TAGGED country, the
    COORD is the error (not the country) — flag it, don't relabel.
  * On apply, record `geo_country_orig` before overwriting `country`, so every
    change is reversible via /undo.

Endpoints (admin-keyed):
  GET  /api/v1/admin/facility-geo/analyze              dry-run report
  POST /api/v1/admin/facility-geo/apply?confirm=1      relabel high-confidence
  POST /api/v1/admin/facility-geo/undo                 revert all relabels
"""
from __future__ import annotations

import os
import re
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger("facility_geo_quality")
facility_geo_quality_bp = Blueprint("facility_geo_quality", __name__)

# Generous country bounding boxes: (min_lat, max_lat, min_lon, max_lon). Kept
# loose enough to place a point, tight enough that a clean single match is a
# confident signal. Overlaps (border regions) resolve to "ambiguous" and are
# never auto-relabeled.
BBOX = {
    "US": (24, 49, -125, -66), "CA": (42, 83, -141, -52), "MX": (14, 33, -118, -86),
    "BR": (-34, 6, -74, -34), "AR": (-55, -21, -74, -53), "CL": (-56, -17, -76, -66),
    "CO": (-4.3, 13, -79, -66), "PE": (-18.5, 0, -81, -68), "EC": (-5, 2, -81, -75),
    "VE": (0.6, 12.5, -73, -59), "UY": (-35, -30, -58.5, -53), "PA": (7, 9.7, -83, -77),
    "CR": (8, 11.3, -86, -82.5), "GT": (13.7, 17.9, -92.3, -88.2),
    "GB": (49.8, 61, -8.3, 2), "IE": (51.3, 55.5, -10.8, -5.3), "FR": (41.3, 51.2, -5.2, 8.3),
    "DE": (47.2, 55.1, 5.8, 15.1), "NL": (50.7, 53.6, 3.3, 7.3), "BE": (49.5, 51.6, 2.5, 6.5),
    "ES": (36, 43.9, -9.4, 3.4), "PT": (36.9, 42.2, -9.6, -6.1), "IT": (36.6, 47.1, 6.6, 18.6),
    "CH": (45.8, 47.9, 5.9, 10.6), "AT": (46.3, 49.1, 9.5, 17.2), "SE": (55.3, 69.1, 11, 24.2),
    "NO": (57.9, 71.3, 4.6, 31.2), "FI": (59.7, 70.1, 20.5, 31.6), "DK": (54.5, 57.8, 8, 12.7),
    "PL": (49, 54.9, 14.1, 24.2), "CZ": (48.5, 51.1, 12, 18.9), "SK": (47.7, 49.6, 16.8, 22.6),
    "HU": (45.7, 48.6, 16.1, 22.9), "RO": (43.6, 48.3, 20.2, 29.7), "GR": (34.8, 41.8, 19.3, 28.3),
    "RU": (41, 78, 27, 180), "UA": (44.3, 52.4, 22.1, 40.2), "TR": (35.8, 42.1, 26, 44.8),
    "BG": (41.2, 44.2, 22.3, 28.6), "HR": (42.4, 46.6, 13.5, 19.4), "RS": (42.2, 46.2, 18.8, 23),
    "LT": (53.9, 56.5, 20.9, 26.9), "LV": (55.6, 58.1, 20.9, 28.2), "EE": (57.5, 59.7, 21.8, 28.2),
    "IS": (63.2, 66.6, -24.6, -13.4), "LU": (49.4, 50.2, 5.7, 6.5),
    "AU": (-44, -10, 112, 154), "NZ": (-47.5, -34, 166, 179), "SG": (1.1, 1.5, 103.5, 104.1),
    "JP": (24, 46, 122, 146), "KR": (33, 39, 124, 132), "CN": (18, 54, 73, 135),
    "HK": (22.1, 22.6, 113.8, 114.5), "TW": (21.9, 25.4, 119.3, 122.1), "IN": (6, 36, 68, 98),
    "ID": (-11, 6.5, 95, 141), "TH": (5, 21, 97, 106), "MY": (0.8, 7.4, 99.6, 119.3),
    "PH": (4.5, 21, 116, 127), "VN": (8.2, 23.5, 102, 110), "PK": (23.5, 37.1, 60.8, 77.8),
    "BD": (20.5, 26.7, 88, 92.7), "LK": (5.8, 9.9, 79.5, 82), "KH": (10, 14.7, 102.3, 107.7),
    "AE": (22.5, 26.2, 51, 56.4), "SA": (16, 32.2, 34.5, 55.7), "IL": (29.4, 33.4, 34.2, 35.9),
    "IR": (25, 39.8, 44, 63.3), "QA": (24.4, 26.2, 50.7, 51.7), "KW": (28.5, 30.1, 46.5, 48.4),
    "ZA": (-35, -22, 16, 33), "NG": (4.2, 13.9, 2.6, 14.7), "KE": (-4.7, 5, 33.9, 41.9),
    "EG": (22, 31.7, 24.7, 36.9), "MA": (27.6, 35.9, -13.2, -1), "TZ": (-11.8, -0.9, 29.3, 40.5),
    # Neighbours of the broad boxes (RU/CN/IN/ZA/BR) so border points resolve to
    # AMBIGUOUS (overlap) instead of a wrong single match — e.g. Tbilisi must not
    # read as Russia. Adding a country can only make a fix MORE conservative.
    "GE": (41, 43.6, 40, 46.8), "AM": (38.8, 41.4, 43.4, 46.7), "AZ": (38.3, 41.9, 44.7, 50.6),
    "KZ": (40.5, 55.5, 46.4, 87.4), "MN": (41.5, 52.2, 87.7, 120), "BY": (51.2, 56.2, 23.1, 32.8),
    "MD": (45.4, 48.5, 26.6, 30.2), "UZ": (37.1, 45.6, 55.9, 73.2), "TM": (35.1, 42.8, 52.4, 66.7),
    "KG": (39.1, 43.3, 69.2, 80.3), "TJ": (36.6, 41.1, 67.3, 75.2), "AF": (29.3, 38.5, 60.4, 74.9),
    "IQ": (29, 37.4, 38.8, 48.6), "SY": (32.3, 37.3, 35.7, 42.4), "JO": (29.1, 33.4, 34.9, 39.3),
    "LB": (33, 34.7, 35.1, 36.6), "OM": (16.6, 26.4, 52, 59.8), "YE": (12.1, 19, 42.5, 53.1),
    "DZ": (18.9, 37.1, -8.7, 12), "TN": (30.2, 37.5, 7.5, 11.6), "LY": (19.5, 33.2, 9.3, 25.2),
    "GH": (4.7, 11.2, -3.3, 1.2), "CI": (4.3, 10.8, -8.6, -2.5), "SN": (12.3, 16.7, -17.6, -11.3),
    "AO": (-18.1, -4.4, 11.6, 24.1), "MZ": (-26.9, -10.4, 30.2, 40.9), "ZW": (-22.5, -15.6, 25.2, 33.1),
    "ZM": (-18.1, -8.2, 21.9, 33.7), "NA": (-28.9, -16.9, 11.7, 25.3), "BW": (-26.9, -17.8, 20, 29.4),
    "SI": (45.4, 46.9, 13.4, 16.6), "MK": (40.8, 42.4, 20.4, 23),
    "AL": (39.6, 42.7, 19.2, 21.1), "BA": (42.5, 45.3, 15.7, 19.6), "ME": (41.8, 43.6, 18.4, 20.4),
    "CY": (34.5, 35.7, 32.2, 34.6), "MT": (35.8, 36.1, 14.1, 14.6), "BO": (-22.9, -9.7, -69.7, -57.5),
    "PY": (-27.6, -19.3, -62.7, -54.3), "PR": (17.9, 18.5, -67.3, -65.6),
    # Small countries / enclaves / territories that broad neighbour boxes swallow
    # (verification caught IM->GB, GF/SR->BR, MO->CN, MM->IN, LS->ZA). Adding them
    # makes those points self-match or resolve to ambiguous, never a wrong relabel.
    "IM": (54.0, 54.42, -4.85, -4.3), "MO": (22.1, 22.22, 113.52, 113.6),
    "GF": (2.1, 5.8, -54.6, -51.6), "SR": (1.8, 6.0, -58.1, -53.9), "GY": (1.1, 8.6, -61.4, -56.5),
    "LS": (-30.7, -28.5, 27.0, 29.5), "SZ": (-27.4, -25.7, 30.7, 32.2),
    "MM": (9.5, 28.6, 92.1, 101.2), "LA": (13.9, 22.6, 100.0, 107.7), "NP": (26.3, 30.5, 80.0, 88.2),
    "BT": (26.7, 28.4, 88.7, 92.2), "BN": (4.0, 5.1, 114.0, 115.4), "TL": (-9.5, -8.1, 124.0, 127.4),
}

# The documented bulk mislabel stamped country='US'. Only rows carrying that
# default are eligible for automatic relabel — a row already tagged with a real
# country (IM, GF, SR, MO, TN…) was set deliberately by its source and is left
# alone even if a neighbour's box would claim its coordinates.
_AUTOFIX_FROM = {"US"}
# A tiny gazetteer of unambiguous major cities per country, used only to catch
# the reverse error — a real US site whose coordinate is a typo — so we relabel
# the country only when the city does NOT vouch for the tagged one.
_CITY_COUNTRY = {}
for _cc, _cities in {
    "US": ("ashburn", "new york", "chicago", "dallas", "atlanta", "san jose", "santa clara",
           "phoenix", "seattle", "portland", "denver", "columbus", "boston", "miami",
           "los angeles", "reston", "sterling", "houston", "las vegas", "omaha"),
}.items():
    for _ct in _cities:
        _CITY_COUNTRY[_ct] = _cc


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _admin_ok():
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    return bool(expected) and provided == expected


def _infer(la, lo):
    """The country whose box uniquely contains the point, else None (a border
    overlap or an unmapped location — never auto-relabeled)."""
    hits = [cc for cc, b in BBOX.items()
            if b[0] <= la <= b[1] and b[2] <= lo <= b[3]]
    return hits[0] if len(hits) == 1 else None


def _city_country(city):
    return _CITY_COUNTRY.get((city or "").strip().lower())


def _scan():
    """Classify every coordinate-bearing row. Returns (fixes, ambiguous, badcoord).
      fixes     — country wrong, coords land uniquely in ONE other country, and
                  the city doesn't vouch for the current country → safe relabel.
      badcoord  — city clearly belongs to the tagged country but coords are
                  elsewhere → the COORDINATE is wrong; flagged, not relabeled.
      ambiguous — coords in overlapping boxes or no box → review, not relabeled."""
    c = _conn()
    if c is None:
        return None
    fixes, ambiguous, badcoord = [], [], []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, country, name, city,
                       COALESCE(latitude, lat), COALESCE(longitude, lon)
                FROM facilities
                WHERE country IS NOT NULL AND country <> ''
                  AND COALESCE(latitude, lat) IS NOT NULL
                  AND COALESCE(longitude, lon) IS NOT NULL
                  AND NOT (COALESCE(latitude, lat) = 0 AND COALESCE(longitude, lon) = 0)
            """)
            for fid, cc, name, city, la, lo in cur.fetchall():
                try:
                    la, lo = float(la), float(lo)
                except Exception:
                    continue
                b = BBOX.get(cc)
                if b and b[0] <= la <= b[1] and b[2] <= lo <= b[3]:
                    continue  # coords agree with tagged country
                inferred = _infer(la, lo)
                rec = {"id": fid, "from": cc, "to": inferred, "name": name,
                       "city": city, "lat": round(la, 3), "lon": round(lo, 3)}
                if _city_country(city) == cc:
                    badcoord.append(rec)          # city says tagged country → bad coord
                elif inferred and inferred != cc and cc in _AUTOFIX_FROM:
                    fixes.append(rec)             # confident relabel (US bulk-default only)
                else:
                    ambiguous.append(rec)         # non-US label or unclear → review
    finally:
        try: c.close()
        except Exception: pass
    return {"fixes": fixes, "ambiguous": ambiguous, "badcoord": badcoord}


def _ensure_columns():
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE facilities ADD COLUMN IF NOT EXISTS geo_country_orig TEXT")
            cur.execute("ALTER TABLE facilities ADD COLUMN IF NOT EXISTS geo_fixed_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE facilities ADD COLUMN IF NOT EXISTS geo_flag TEXT")
    except Exception as e:
        logger.warning("[geo] ensure columns failed: %s", str(e)[:140])
    finally:
        try: c.close()
        except Exception: pass


@facility_geo_quality_bp.route("/api/v1/admin/facility-geo/analyze")
def geo_analyze():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    s = _scan()
    if s is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    from collections import Counter
    flows = Counter((r["from"], r["to"]) for r in s["fixes"]).most_common(25)
    sample = request.args.get("sample") == "1"
    return jsonify(ok=True, dry_run=True,
                   fixable=len(s["fixes"]), ambiguous=len(s["ambiguous"]),
                   badcoord=len(s["badcoord"]),
                   top_flows=[{"from": f[0], "to": f[1], "n": n} for f, n in flows],
                   fixes_sample=s["fixes"][:40] if sample else None), 200


@facility_geo_quality_bp.route("/api/v1/admin/facility-geo/apply", methods=["POST"])
def geo_apply():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    s = _scan()
    if s is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True, would_fix=len(s["fixes"]),
                       note="add ?confirm=1 to relabel"), 200
    _ensure_columns()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    import psycopg2.errors
    fixed = flagged = crosscountry = 0
    try:
        with c.cursor() as cur:
            for r in s["fixes"]:
                try:
                    cur.execute(
                        "UPDATE facilities SET geo_country_orig=COALESCE(geo_country_orig, country), "
                        "country=%s, geo_fixed_at=NOW(), geo_flag='relabeled_from_coords' "
                        "WHERE id=%s AND country='US'", (r["to"], r["id"]))
                    fixed += cur.rowcount
                except psycopg2.errors.UniqueViolation:
                    # relabel collides with a real twin in the target country →
                    # this mislabeled row is a cross-country duplicate; hide it.
                    try:
                        cur.execute(
                            "SELECT id FROM facilities t WHERE t.country=%s "
                            "AND t.name=(SELECT name FROM facilities WHERE id=%s) "
                            "AND COALESCE(t.city,'')=(SELECT COALESCE(city,'') FROM facilities WHERE id=%s) "
                            "AND t.id<>%s LIMIT 1", (r["to"], r["id"], r["id"], r["id"]))
                        tw = cur.fetchone()
                        if tw:
                            cur.execute(
                                "UPDATE facilities SET duplicate_of_id=%s, "
                                "dedup_method='geo_crosscountry', dedup_at=NOW(), "
                                "geo_flag='crosscountry_dup' WHERE id=%s", (tw[0], r["id"]))
                            crosscountry += cur.rowcount
                    except Exception:
                        pass
                except Exception:
                    pass
            for r in s["badcoord"]:
                try:
                    cur.execute("UPDATE facilities SET geo_flag='suspect_coord' "
                                "WHERE id=%s AND geo_flag IS NULL", (r["id"],))
                    flagged += cur.rowcount
                except Exception:
                    pass
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, relabeled=fixed, crosscountry_dups=crosscountry,
                   coord_flagged=flagged, ambiguous_left=len(s["ambiguous"])), 200


@facility_geo_quality_bp.route("/api/v1/admin/facility-geo/undo", methods=["POST"])
def geo_undo():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    n = 0
    try:
        with c.cursor() as cur:
            cur.execute("UPDATE facilities SET country=geo_country_orig, geo_country_orig=NULL, "
                        "geo_fixed_at=NULL, geo_flag=NULL "
                        "WHERE geo_country_orig IS NOT NULL")
            n = cur.rowcount
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, reverted=n), 200


# ===========================================================================
# r-discovered-country (2026-08-07) — the SAME defect, in the OTHER table.
# ===========================================================================
# The 2026-07-20 pass above scans and repairs `facilities`. It never touches
# `discovered_facilities` — grep this file before this block and the count is
# literally zero. That matters because the two tables are read by different
# things: `facilities` is the canonical merged table, while
# `discovered_facilities` is the public-search index AND the table every
# market-scoped DCPI query reads through `_market_country_scope`
# (routes/dcpi.py). So the bulk `country='US'` mislabel was cleaned where it
# was invisible and left in place where it is load-bearing.
#
# It became load-bearing on 2026-08-07. Before PRs #2367/#2377 a market-scoped
# facility query had no country predicate, so a mis-countried row still showed
# up on its own market's page (the city string carried it). Now every such
# query is country-scoped, and a row whose country contradicts its city simply
# vanishes from the market it belongs to. Measured on the live table: 78 rows,
# restoring 12 facility appearances across /dcpi/london (+7), /dcpi/toronto
# (+2), /dcpi/tokyo (+2) and /dcpi/manchester (+1).
#
# WHY THIS IS NOT JUST `_scan()` POINTED AT A SECOND TABLE — three sub-classes
# exist and the bounding box only sees the first:
#
#   A. Coords fall OUTSIDE the tagged country's own box. The box disproves the
#      label; `_infer` (or, where boxes overlap, a neighbour vote) names the
#      replacement. 73 rows. This is what `_scan()` already does.
#
#   B. Coords fall INSIDE the tagged country's box and the label is still
#      wrong, because the boxes are deliberately generous: US spans 24–49N /
#      125–66W, which swallows Toronto, Montreal, Vancouver and Windsor. The
#      reported case — Equinix TR2, city='Toronto', state='NY', country='US',
#      coordinates in downtown Toronto — is bbox-INVISIBLE. Caught instead by
#      requiring TWO independent votes to agree: the other facilities within
#      25 km, and the other rows sharing this row's city name. 3 rows.
#
#   C. No coordinates at all. "DREAM CLOUD Tokyo #1" carries country='US',
#      city='Tokyo' and NULL lat/lon, so no coordinate check can ever reach
#      it. Only the city gazetteer can. 2 rows.
#
# WHAT IS DELIBERATELY *NOT* AUTO-FIXED, each because it produced a wrong
# answer when it was tried against the live table:
#
#   * A tagged country with no BBOX entry is never relabeled. Trinidad,
#     Rwanda, El Salvador, Malawi, Burundi, Kosovo and the Bahamas have no box
#     of their own, so their coordinates land in a neighbour's and read as a
#     confident single match. That alone was 41 false positives.
#   * Only `_AUTOFIX_FROM` (i.e. 'US') is relabeled automatically, for the
#     reason the original pass documented: 'US' is what a bulk importer stamps
#     by DEFAULT, so it can be outvoted; 'RU' or 'IN' was set deliberately and
#     is left for review. This is what keeps TiS-Dialog in KALININGRAD tagged
#     RU — a Russian exclave at 20.5E, outside the RU box, which the box
#     "disproves" and is wrong to.
#   * Rule B needs the neighbour vote AND the city vote to name the SAME
#     country. Neighbours alone flip Goyeau Data Centre in Windsor ON to US
#     (31 of its 32 neighbours are in Detroit) and TKRZ Nordhorn to NL. The
#     city vote vetoes both. It is also what separates the ten Singapore-city
#     rows tagged MY from the six genuine Johor Bahru ones 20 km away.
#   * Rule C abstains when the row's own NAME is itself a city name different
#     from its `city`. `source='providerwebsites'` scraped Equinix's site
#     navigation into 300+ rows like name='Chicago', city='London',
#     coords (0,0) — page titles, not facilities. Their `country` is the one
#     field that is right; the broken field is `city`. Without this guard rule
#     C relabeled 22 of them US->GB. (Their `city` is a separate defect, out
#     of scope here — flagged in the report as `name_is_city`.)
#   * A row never votes on its own label: the city vote discounts one row of
#     the row's own tagged country before counting. This only changes an
#     outcome at the threshold boundary (17 Toronto rows tagged CA against 2
#     tagged US is 0.895 counting yourself and 0.944 not), but that is exactly
#     where a mislabeled row would otherwise suppress the evidence against it.
#
# CONTROL that must keep passing: "SD Data Center", city='Melbourne',
# state='FL', coords 28.26N/-80.69W is Melbourne FLORIDA. Its absence from
# /dcpi/melbourne (the AEMO/VIC market) is CORRECT — that is r-namesake
# working, not a bug. Melbourne is the reason the city gazetteer can never
# outrank a coordinate: 57 AU rows at -37.8 and 2 US rows at +28.26 share the
# string. See tests/test_discovered_country_repair.py.

_DF_RADIUS_KM = 25.0     # a metro, not a country
_DF_MIN_VOTES = 5        # below this a "consensus" is noise
_DF_AGREE = 0.90         # near-unanimity, since one bad flip re-creates the bug
_DF_CELL = 0.25          # spatial bucket, degrees
_DF_FLAG = "df_relabeled_from_coords"   # only /undo's own writes are reverted


def _df_usable(la, lo):
    """(0,0) is this table's 'unknown', not a point in the Gulf of Guinea."""
    return la is not None and lo is not None and not (la == 0 and lo == 0)


def _df_rows(cur):
    cur.execute("""
        SELECT id, name, city, state, UPPER(COALESCE(country, '')),
               latitude, longitude, duplicate_of_id
          FROM discovered_facilities
    """)
    out = []
    for i, n, ci, st, cc, la, lo, dup in cur.fetchall():
        try:
            la = float(la) if la is not None else None
            lo = float(lo) if lo is not None else None
        except (TypeError, ValueError):
            la = lo = None
        out.append({"id": i, "name": n, "city": ci, "state": st, "cc": cc,
                    "lat": la, "lon": lo, "dup": dup})
    return out


def _df_scan():
    """Fetch + classify. The classification itself is _df_classify, kept pure
    so tests/test_discovered_country_repair.py can drive it on synthetic rows
    with no database."""
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            rows = _df_rows(cur)
    finally:
        try: c.close()
        except Exception: pass
    return _df_classify(rows)


def _df_classify(rows):
    """Classify every discovered_facilities row. See the block comment above
    for why each guard exists — every one of them is load-bearing against a
    measured false positive."""
    import math
    from collections import Counter, defaultdict

    coord = [r for r in rows if _df_usable(r["lat"], r["lon"])]

    grid = defaultdict(list)
    for r in coord:
        grid[(int(r["lat"] // _DF_CELL), int(r["lon"] // _DF_CELL))].append(r)

    # City gazetteer AND the set of strings that are known city names, both
    # built only from rows that carry real coordinates — a row with no
    # coordinates has no business voting on where a city is.
    gaz = defaultdict(Counter)
    for r in coord:
        if r["cc"] and r["city"]:
            gaz[r["city"].strip().lower()][r["cc"]] += 1

    def neighbours(r):
        dla = _DF_RADIUS_KM / 111.0
        dlo = _DF_RADIUS_KM / max(1.0, 111.0 * math.cos(math.radians(r["lat"])))
        out = []
        for gy in range(int((r["lat"] - dla) // _DF_CELL),
                        int((r["lat"] + dla) // _DF_CELL) + 1):
            for gx in range(int((r["lon"] - dlo) // _DF_CELL),
                            int((r["lon"] + dlo) // _DF_CELL) + 1):
                for o in grid.get((gy, gx), ()):
                    if (o["id"] != r["id"] and o["cc"]
                            and abs(o["lat"] - r["lat"]) <= dla
                            and abs(o["lon"] - r["lon"]) <= dlo):
                        out.append(o)
        return out

    def vote(counter, total):
        if total < _DF_MIN_VOTES:
            return None
        top, n = counter.most_common(1)[0]
        return top if n / total >= _DF_AGREE else None

    def city_vote(r):
        """Dominant country for this row's city, discounting the row's own
        vote so it cannot be evidence for its own label."""
        v = Counter(gaz.get((r["city"] or "").strip().lower(), {}))
        if not v:
            return None
        if v.get(r["cc"]):
            v[r["cc"]] -= 1
        return vote(v, sum(v.values()))

    fixes, review, abstain = [], [], []
    unmapped = name_is_city = 0

    for r in rows:
        cc = r["cc"]
        if not cc:
            continue
        if cc not in BBOX:
            unmapped += 1            # cannot disprove what we cannot locate
            continue

        to = rule = ev = None
        if _df_usable(r["lat"], r["lon"]):
            b = BBOX[cc]
            inside = b[0] <= r["lat"] <= b[1] and b[2] <= r["lon"] <= b[3]
            nb = neighbours(r)
            nv = vote(Counter(o["cc"] for o in nb), len(nb)) if nb else None
            if not inside:
                cand = _infer(r["lat"], r["lon"]) or nv
                if cand and cand != cc:
                    to, rule = cand, "A/bbox-disproof"
                    ev = "coords outside %s box" % cc
            elif nv and nv != cc:
                cv = city_vote(r)
                if cv == nv:
                    to, rule = nv, "B/border-consensus"
                    ev = "%d/%d neighbours and city '%s' both say %s" % (
                        sum(1 for o in nb if o["cc"] == nv), len(nb), r["city"], nv)
                else:
                    abstain.append(dict(r, to=nv, why="neighbours say %s, city vote %s"
                                        % (nv, cv)))
        elif r["city"]:
            # No coordinates: the city string is the only evidence there is, so
            # refuse it when the row's own name names a DIFFERENT city.
            if (r["name"] or "").strip().lower() in gaz:
                name_is_city += 1
                continue
            cv = city_vote(r)
            if cv and cv != cc:
                to, rule = cv, "C/city-gazetteer"
                ev = "no coords; city '%s' resolves to %s" % (r["city"], cv)

        if not to:
            continue
        rec = {"id": r["id"], "from": cc, "to": to, "name": r["name"],
               "city": r["city"], "state": r["state"], "rule": rule,
               "evidence": ev, "lat": r["lat"], "lon": r["lon"]}
        (fixes if cc in _AUTOFIX_FROM else review).append(rec)

    return {"fixes": fixes, "review": review, "abstain": abstain,
            "unmapped_tag": unmapped, "name_is_city": name_is_city}


@facility_geo_quality_bp.route("/api/v1/admin/facility-geo/discovered/analyze")
def geo_discovered_analyze():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    s = _df_scan()
    if s is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    from collections import Counter
    return jsonify(
        ok=True, dry_run=True, table="discovered_facilities",
        fixable=len(s["fixes"]), review=len(s["review"]),
        abstain=len(s["abstain"]), unmapped_tag=s["unmapped_tag"],
        name_is_city=s["name_is_city"],
        by_rule=dict(Counter(r["rule"] for r in s["fixes"])),
        flows=[{"from": f, "to": t, "n": n} for (f, t), n in
               Counter((r["from"], r["to"]) for r in s["fixes"]).most_common(25)],
        fixes=s["fixes"] if request.args.get("full") == "1" else s["fixes"][:40],
        review_rows=s["review"] if request.args.get("full") == "1" else s["review"][:20],
    ), 200


@facility_geo_quality_bp.route("/api/v1/admin/facility-geo/discovered/apply",
                               methods=["POST"])
def geo_discovered_apply():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    s = _df_scan()
    if s is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True, would_fix=len(s["fixes"]),
                       note="add ?confirm=1 to relabel"), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    fixed = 0
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE discovered_facilities "
                        "ADD COLUMN IF NOT EXISTS geo_country_orig TEXT")
            # geo_flag is what makes /undo reversible SAFELY: 48 rows already
            # carried geo_country_orig before this endpoint existed (written by
            # an earlier process), and an undo scoped on that column alone
            # would revert their work too.
            cur.execute("ALTER TABLE discovered_facilities "
                        "ADD COLUMN IF NOT EXISTS geo_flag TEXT")
            for r in s["fixes"]:
                try:
                    # The country=%s guard makes this idempotent and makes a
                    # concurrent re-label a no-op rather than a clobber.
                    cur.execute(
                        "UPDATE discovered_facilities "
                        "SET geo_country_orig = COALESCE(geo_country_orig, country), "
                        "    country = %s, geo_flag = %s "
                        "WHERE id = %s AND UPPER(COALESCE(country,'')) = %s",
                        (r["to"], _DF_FLAG, r["id"], r["from"]))
                    fixed += cur.rowcount
                except Exception as e:
                    logger.warning("[geo-df] id=%s failed: %s", r["id"], str(e)[:120])
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, table="discovered_facilities", relabeled=fixed,
                   review_left=len(s["review"]), abstain=len(s["abstain"])), 200


@facility_geo_quality_bp.route("/api/v1/admin/facility-geo/discovered/undo",
                               methods=["POST"])
def geo_discovered_undo():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    n = 0
    try:
        with c.cursor() as cur:
            cur.execute("UPDATE discovered_facilities "
                        "SET country = geo_country_orig, geo_country_orig = NULL, "
                        "    geo_flag = NULL "
                        "WHERE geo_flag = %s AND geo_country_orig IS NOT NULL",
                        (_DF_FLAG,))
            n = cur.rowcount
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, reverted=n), 200
