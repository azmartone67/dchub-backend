"""
Facility profile page — dynamic HTML renderer (2026-05-19).

Closes the gap user spotted: there are 2,002 static HTML files in
dchub-frontend/facilities/ but ~21,000 facilities in the DB. >90% of
facility profiles 404. Adding/discovering new facilities silently
broke their profiles.

This route renders any facility on demand:
  GET /facilities/<slug>      — HTML profile page
  GET /facilities/<slug>.html — same (handles .html-suffix from old links)

The renderer pulls facility data via the same query the
/api/v1/facilities/<slug> endpoint uses (slug = name-with-dashes +
8-char MD5 hash of id), then emits HTML that matches the existing
static file style.

CF Pages serves static files first via _routes.json. If a static file
exists for a facility, it wins. This route only fires when the static
file doesn't exist (CF Pages 404 falls through to the worker, which
forwards to backend via PHASE_282_RAILWAY_PATHS prefix match).
"""

import math
import os
from util.iso_taxonomy import is_registered_label as _is_registered_label
from routes.url_registry import build_public_url
from ai_surface_canon import PINNED as _CANON
import logging
from flask import Blueprint, request, Response, jsonify
import datetime as _dt

logger = logging.getLogger(__name__)
facility_profile_bp = Blueprint("facility_profile", __name__)


def _fetch_facility_by_slug(slug: str) -> dict | None:
    """Same slug-hash lookup as /api/v1/facilities/<slug>."""
    parts = slug.rsplit("-", 1)
    if len(parts) != 2 or len(parts[1]) != 8:
        return None
    hash8 = parts[1]
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn: return None
        try:
            from routes.facility_slug import hash_sql
            c = conn.cursor()
            # r-ner-noindex (2026-08-09): warm the published-NER slug set on
            # the connection we already hold, BEFORE the row lookup. Placing
            # it first is the whole point — _render_profile reads the set
            # further down this same request, so there is no cold window in
            # which the first hit on a junk page renders index,follow. ~62
            # slugs, one refresh per process per hour; its own try/except and
            # its own rollback so it can never cost us the facility row.
            try:
                from util.facility_ner_noindex import refresh_suppressed_slugs
                refresh_suppressed_slugs(c)
            except Exception as _ner_err:
                try: conn.rollback()
                except Exception: pass
                logger.warning("facility_profile: NER-noindex set unavailable "
                               "(%s) — those pages stay indexed", _ner_err)
            # ★ is_duplicate + duplicate_of_id are SELECTED, not decoration:
            # the twin-canonical branch reads them off this row. Without them
            # fac.get("is_duplicate") is always None and the branch can never
            # fire — verified live, the fix shipped inert until this line.
            #
            # ★ r-frozen-slug-select (2026-08-09): canonical_slug is the SAME
            # class of bug, and it shipped inert for 34 days. _render_profile
            # does `fac.get("canonical_slug") or slug` to (a) emit rel=canonical
            # at the FROZEN slug and (b) key _is_junk_facility's noindex test.
            # canonical_slug was matched on in the WHERE below but never put in
            # the column list, so .get() was always None and BOTH protections
            # silently degraded to "whatever slug the request arrived on".
            # Measured live on prod 2026-08-09, before this line:
            #   /facilities/totally-bogus-alias-name-26f01f95 → 200,
            #       <link rel=canonical .../totally-bogus-alias-name-26f01f95>
            #       i.e. an unbounded family of alias URLs each declaring
            #       ITSELF canonical — precisely the index-signal split the
            #       r-frozen-slug comment was written to prevent.
            #   /facilities/zzz-alias-07a85c97 → 200 + robots="index, follow",
            #       while its frozen twin /facilities/copilot-07a85c97 → noindex.
            #       That is the "KNOWN, DELIBERATE GAP" in the header of
            #       util/facility_ner_noindex.py; it was never a property of
            #       the NER slug set, only of this missing column.
            #
            # ★ PROBE PER TABLE (the 2026-07-03 pattern at ~356 / ~433). Naming
            # a column that does not exist is NOT uniformly fail-soft here:
            #   · the discovered_facilities hash8 fallback below carries no
            #     try/except of its own, so it would raise into the outer
            #     handler and return None — a 404 on EVERY facility page;
            #   · the `facilities` fallback is wrapped, so it would just go
            #     quiet and 404 legacy-only facilities.
            # A wrong-negative probe is free: the exact-match loop matched on
            # canonical_slug = slug, so slug already IS the frozen slug there,
            # and the fallbacks simply keep today's behaviour.
            _has_canon = {}
            for _t in ("discovered_facilities", "facilities"):
                try:
                    c.execute("SELECT 1 FROM information_schema.columns "
                              "WHERE table_name=%s "
                              "AND column_name='canonical_slug'", (_t,))
                    _has_canon[_t] = c.fetchone() is not None
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    _has_canon[_t] = False
            _cs = {t: ("canonical_slug" if ok else "NULL AS canonical_slug")
                   for t, ok in _has_canon.items()}
            # LANE 1: same probe, same reason. substation_band is added by
            # routes/substation_band_producer.py's admin endpoint, NOT at boot,
            # so between deploying this code and POSTing the backfill the
            # column legitimately does not exist. Probing keeps that window a
            # no-op (band reads as None → _infra_rows returns []) instead of a
            # 404 on every facility page.
            _has_band = {}
            for _t in ("discovered_facilities", "facilities"):
                try:
                    c.execute("SELECT 1 FROM information_schema.columns "
                              "WHERE table_name=%s "
                              "AND column_name='substation_band'", (_t,))
                    _has_band[_t] = c.fetchone() is not None
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    _has_band[_t] = False
            _sb = {t: ("substation_band" if ok else "NULL AS substation_band")
                   for t, ok in _has_band.items()}
            _cols = ("id, name, provider, city, state, country, {region}, "
                     "latitude, longitude, power_mw, status, address, "
                     "is_duplicate, duplicate_of_id, {cs}, {sb}")
            # r-slug-freeze (2026-07-03): exact match on the FROZEN
            # canonical_slug column FIRST — indexed, and immune to the
            # name/provider drift that recomputing MD5(provider|name) live
            # introduces (the root cause of the GSC indexing churn). Falls
            # through to the live-hash match below for rows not yet backfilled
            # or if the column doesn't exist yet (pre-migration).
            row = None
            for _tbl, _region in (("discovered_facilities", "market AS region"),
                                  ("facilities", "NULL AS region")):
                try:
                    c.execute(
                        "SELECT " + _cols.format(region=_region,
                                                 cs=_cs[_tbl],
                                                 sb=_sb[_tbl]) +
                        f" FROM {_tbl} WHERE canonical_slug = %s"
                        " ORDER BY COALESCE(power_mw, 0) DESC, id ASC LIMIT 1",
                        (slug,))
                    row = c.fetchone()
                    if row:
                        break
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    row = None
            if row:
                return dict(zip([d[0] for d in c.description], row))
            # r-stable-slug: stable provider|name hash can collide; ORDER BY
            # highest power then lowest id so a collision deterministically
            # resolves to the canonical facility.
            # ★ This is THE path the frozen-slug canonical exists for: it keys
            # on hash8 alone and ignores the slug's name-part entirely, so any
            # /facilities/<anything>-<hash8> resolves here. Without
            # canonical_slug selected, every one of those served a
            # self-canonical. Probed expression — see _has_canon above; this
            # execute() has no try/except, so a bad column list 404s the site.
            c.execute("""
                SELECT id, name, provider, city, state, country,
                       market AS region, latitude, longitude,
                       power_mw, status, address,
                       is_duplicate, duplicate_of_id,
                       """ + _cs["discovered_facilities"] + """,
                       """ + _sb["discovered_facilities"] + """
                FROM discovered_facilities
                WHERE """ + hash_sql('') + """ = %s
                ORDER BY COALESCE(power_mw, 0) DESC, id ASC
                LIMIT 1
            """, (hash8,))
            row = c.fetchone()
            # r-facility-301 (2026-07-03): second-chance lookup in the curated
            # legacy `facilities` table (~15.8k rows, TEXT hex/osm ids), same
            # slug-hash scheme. The legacy /facility/<id> pages now 301 HERE
            # (routes/seo_pages.py facility_page), so a legacy-only facility —
            # one with no discovered_facilities twin sharing provider|name —
            # must resolve or the redirect lands on a 404. Mirrors the
            # 2026-07-01 d495c8bd fix on the /facility/<id> side.
            if not row:
                try:
                    c.execute("""
                        SELECT id, name, provider, city, state, country,
                               NULL AS region, latitude, longitude,
                               power_mw, status, address,
                               NULL AS is_duplicate, NULL AS duplicate_of_id,
                               """ + _cs["facilities"] + """,
                               """ + _sb["facilities"] + """
                        FROM facilities
                        WHERE """ + hash_sql('') + """ = %s
                        ORDER BY COALESCE(power_mw, 0) DESC, id ASC
                        LIMIT 1
                    """, (hash8,))
                    row = c.fetchone()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    row = None
            if not row: return None
            cols = [desc[0] for desc in c.description]
            return dict(zip(cols, row))
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"facility_profile fetch failed: {e}")
        return None


# r-market-resolve-geo (2026-08-26): both thresholds are MEASURED, not chosen.
#
# Over a random 500 of the 9,095 sitemap facility pages, each page's rendered
# market was compared against that page's own published coordinates:
#
#     correctly-resolved             median 3 km, p90 33 km, p95 119 km
#     the whole legitimate tail      69 73 77 81 86 90 92 119 121 152 169 201
#     the bug cluster                7395 7486 7489 7749 8616
#
# NOTHING lands between 201 km and 7,395 km, so any cut inside that gap
# separates a real market from a state-code collision. The cluster is entirely
# Brazilian and the cause is steps (3)/(4) filtering on a bare `state` string
# with no country and no distance guard: BR state SC matched Charleston SC, MT
# matched Billings (Montana), ES matched Madrid (Spain). Those pages print a US
# grid operator in a Brazilian facility's <title> — "SERC grid" on a Blumenau
# data center — and splice in a RAG narrative about the wrong continent.
_NEAR_KM = 150.0    # a market this close may be claimed as the facility's own
_SANITY_KM = 400.0  # past this, a match is a collision, not a market


def _km_between(lat1, lon1, lat2, lon2):
    """Great-circle km, or None if either point is unusable.

    Deliberately NOT the flat-earth metric used for SQL ordering below: that
    one only has to rank candidates, this one has to tell 200 km from 7,400 km
    reliably at any latitude, because a market is accepted or rejected on it.
    """
    try:
        p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
        dphi = math.radians(float(lat2) - float(lat1))
        dlam = math.radians(float(lon2) - float(lon1))
        a = (math.sin(dphi / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
        return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))
    except (TypeError, ValueError):
        return None


def _market_dcpi(city: str, state: str, lat=None, lng=None) -> dict | None:
    """Best-effort DCPI verdict for the facility's market so the profile shows
    real intelligence, not just sparse metadata.

    Resolution order (r-market-resolve 2026-07-06, extended 2026-08-26):
    (1) exact city/metro slug match; (2) the geographically NEAREST metro in
    the same state by lat/lng; (3) the nearest market to the facility's OWN
    coordinates anywhere on earth, within _NEAR_KM; (4) otherwise the
    most-recent row in the state. Same-state before global is deliberate and
    measured — see the ordering note in the body. Every one of them is now rejected if the market it names is further
    than _SANITY_KM from the facility.

    The old single-query `market_slug OR state ... ORDER BY computed_at`
    collapsed every facility in a state onto one arbitrary (most-recently-
    computed) metro — e.g. a Dallas facility resolving to Midland-Odessa
    (/dcpi/midland-tx) even when a `dallas` row existed, because the state
    clause returned it too and a fresher computed_at won the tie. The RAG
    "Market context" splice (93037e04) now names the resolved market
    prominently, so a wrong market is user- and SEO-visible.
    See memory reference_dchub_market_slugs (markets=METRO, dcpi=CITY).

    ★ STEP (2) IS WHY INTERNATIONAL PAGES WERE THIN. Every fallback used to be
      gated on `state`, a US-shaped field, and the function returned None
      outright when city and state were both empty — throwing away a perfectly
      good pair of coordinates. Measured on the same 500-page sample: 74.4% of
      US facility pages carry market context against 24.0% of non-US ones, and
      a page with context runs a median 474 visible words against 224 without.
      All 81 pages under 200 words in that sample lacked market context; only
      half of them lacked a city. Coordinates are present on 78% of pages.
    """
    _COLS = ("market_slug, market_name, iso, verdict, "
             "excess_power_score, constraint_score, time_to_power_months")
    # latitude/longitude are read only to police the distance guard and are
    # popped before returning, so the dict handed to the page is unchanged.
    _SEL = _COLS + ", latitude, longitude"

    # City/metro slug candidates. The bare state code is deliberately NOT here —
    # it belongs only to the geographic fallback below, never the exact match.
    city_cands = []
    if city:
        base = city.lower().split(",")[0].strip().replace(" ", "-")
        if base:
            city_cands.append(base)
        full = city.lower().replace(" ", "-")
        if full:
            city_cands.append(full)
        if base and state and len(state.strip()) == 2:
            # bulk_dcpi_score stores some slugs as '<city>-<st>' (e.g. st-louis-mo)
            city_cands.append(f"{base}-{state.strip().lower()}")
    city_cands = list(dict.fromkeys(c for c in city_cands if c))  # order-preserving de-dupe
    st = (state or "").strip().lower()

    # Facility coords. Coerce defensively — fac dict values can be str/Decimal/
    # None. Parsed BEFORE the give-up check because coordinates are now a
    # resolution path in their own right, not just a tie-breaker.
    try:
        flat = float(lat) if lat not in (None, "") else None
        flng = float(lng) if lng not in (None, "") else None
    except (TypeError, ValueError):
        flat = flng = None
    if flat is not None and not (-90.0 <= flat <= 90.0):
        flat = flng = None
    if flng is not None and not (-180.0 <= flng <= 180.0):
        flat = flng = None

    if not city_cands and not st and flat is None:
        return None

    def _too_far(row, limit):
        """True only when the row names a market DEMONSTRABLY too far away.

        Unknown distance is not far: a facility with no coordinates, or a
        market row with none, keeps the pre-2026-08-26 behaviour rather than
        losing the context it has today.
        """
        if not row or flat is None or flng is None:
            return False
        d = _km_between(flat, flng, row.get("latitude"), row.get("longitude"))
        return d is not None and d > limit

    def _clean(row):
        if row:
            row.pop("latitude", None)
            row.pop("longitude", None)
        return row

    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return None
        try:
            c = conn.cursor()

            def _fetch(sql, params):
                c.execute(sql, params)
                r = c.fetchone()
                return dict(zip([d[0] for d in c.description], r)) if r else None

            # (1) Exact city/metro slug match. Constrain to the facility's state
            # (when known) so same-name cities in other states can't collide.
            # The `state IS NULL OR` arm is a real hole — a coordinate-less
            # /dcpi/athens (Greece) row satisfies it for a facility in Athens,
            # Georgia — so the match is now distance-checked like every other.
            if city_cands:
                if st:
                    row = _fetch(
                        f"SELECT {_SEL} FROM market_power_scores "
                        "WHERE LOWER(market_slug) = ANY(%s) "
                        "  AND (state IS NULL OR LOWER(state) = %s) "
                        "ORDER BY computed_at DESC LIMIT 1",
                        (city_cands, st))
                else:
                    row = _fetch(
                        f"SELECT {_SEL} FROM market_power_scores "
                        "WHERE LOWER(market_slug) = ANY(%s) "
                        "ORDER BY computed_at DESC LIMIT 1",
                        (city_cands,))
                if row and not _too_far(row, _SANITY_KM):
                    return _clean(row)

            # ★ ORDER MATTERS, AND IT IS ABOUT THE GRID, NOT THE DISTANCE.
            # r-market-resolve-geo shipped the coordinate step ABOVE this one
            # and measured the result on 500 live pages: three US facilities
            # moved to a NEARER market across a grid boundary and their <title>
            # changed operator with them —
            #
            #   Microsoft Boydton, VA    /dcpi/chester (PJM)  -> /dcpi/durham (SERC)
            #   Microsoft Azure East US  /dcpi/chester (PJM)  -> /dcpi/durham (SERC)
            #   Meta Jeffersonville, IN  /dcpi/indianapolis   -> /dcpi/louisville
            #                                        (MISO)              (SERC)
            #
            # Boydton is Dominion territory inside PJM; Durham is Duke Progress,
            # which is not in an RTO at all. Jeffersonville is MISO, Louisville
            # across the river is not. That is the SAME defect class the geo fix
            # exists to kill — a facility wearing another grid's operator — just
            # at 90 km instead of 7,395. Nearest is the right tie-break WITHIN a
            # grid and the wrong one ACROSS it, so the same-state pick now runs
            # first: `state` is a poor country signal (that is what collided)
            # but a decent ISO proxy inside the US, and it restores exactly the
            # answer these pages had before. The global step keeps everything it
            # was added for — it still owns every facility with no usable state,
            # which is the entire international case it was written for.

            # (2) Nearest metro IN THE SAME STATE by lat/lng. Local flat-earth
            # metric: weight longitude by cos(latitude) so E-W and N-S degrees
            # are comparable. Only relative ordering matters, so squared distance
            # is fine (no sqrt). Coord coverage in market_power_scores is sparse —
            # e.g. in TX only Midland-Odessa carries coords while Dallas/Houston/
            # Austin are NULL — so a SINGLE coord-bearing metro must NOT be
            # treated as "nearest" to every uncovered city in the state (that's
            # the exact Dallas->Midland collapse we're fixing). Require >=2
            # coord-bearing metros before trusting the geographic pick; otherwise
            # ranking on one point is meaningless and we defer on.
            if st and flat is not None and flng is not None:
                c.execute(
                    f"SELECT {_SEL} FROM market_power_scores "
                    "WHERE LOWER(state) = %s "
                    "  AND latitude IS NOT NULL AND longitude IS NOT NULL "
                    "ORDER BY (POWER(latitude - %s, 2) + "
                    "          POWER((longitude - %s) * COS(RADIANS(%s)), 2)) ASC "
                    "LIMIT 2",
                    (st, flat, flng, flat))
                rows = c.fetchall()
                if len(rows) >= 2:
                    row = dict(zip([d[0] for d in c.description], rows[0]))
                    if not _too_far(row, _SANITY_KM):
                        return _clean(row)

            # (3) Nearest market to the facility's own coordinates, ANYWHERE —
            # no state, so this is the path that works outside the US. Bounded
            # by a lat/lon box first: the box keeps the scan small and lets an
            # index on (latitude, longitude) do the work instead of sorting
            # every scored market on a computed expression. The box CIRCUM-
            # SCRIBES the circle, so a corner hit can be up to _NEAR_KM*sqrt(2)
            # away — the _too_far check trims it back to a true radius.
            # Near the antimeridian the box does not wrap; that fails CLOSED
            # (no candidate, fall through) and never mismatches.
            if flat is not None and flng is not None:
                dlat = _NEAR_KM / 111.32
                _cos = math.cos(math.radians(flat))
                dlng = (_NEAR_KM / (111.32 * _cos)) if abs(_cos) > 1e-6 else 180.0
                row = _fetch(
                    f"SELECT {_SEL} FROM market_power_scores "
                    "WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
                    "  AND latitude BETWEEN %s AND %s "
                    "  AND longitude BETWEEN %s AND %s "
                    "ORDER BY (POWER(latitude - %s, 2) + "
                    "          POWER((longitude - %s) * COS(RADIANS(%s)), 2)) ASC, "
                    "         computed_at DESC LIMIT 1",
                    (flat - dlat, flat + dlat, flng - dlng, flng + dlng,
                     flat, flng, flat))
                if row and not _too_far(row, _NEAR_KM):
                    return _clean(row)

            if not st:
                return None

            # (4) Fallback: most-recent row in the state (legacy behavior — used
            # only when we have no city match and no usable coords).
            row = _fetch(
                f"SELECT {_SEL} FROM market_power_scores "
                "WHERE LOWER(state) = %s "
                "ORDER BY computed_at DESC LIMIT 1",
                (st,))
            return _clean(row) if not _too_far(row, _SANITY_KM) else None
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"facility_profile dcpi failed: {e}")
        return None


def _esc(s) -> str:
    """HTML-escape."""
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _ascii_header(v) -> str:
    """ASCII-only header value. HTTP headers must be latin-1 — an em-dash in
    X-Cite-As made gunicorn reject EVERY response from /api/v1/industry/pulse
    (502 since launch; see routes/industry_pulse.py). Never raises."""
    try:
        return str(v or "").encode("ascii", "ignore").decode("ascii")
    except Exception:
        return ""


def _slugify(text: str) -> str:
    import re as _re2
    return _re2.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _legacy_name_part(provider, name) -> str:
    """The PRE-dedupe name-part — unconditional `{provider}-{name}` under this
    module's historical slugify. Used ONLY to MATCH legacy indexed slugs in
    _resolve_legacy_slug: those were composed exactly this way (doubled brand
    prefix and all), so matching them against today's deduped form would break
    tier-0 for precisely the doubled-name population the resolver exists to
    save (validated 95% recovery / 0 mis-redirects). Emitters must NOT use
    this — they go through _fac_slug."""
    ps, ns = _slugify(provider or ""), _slugify(name or "")
    return f"{ps}-{ns}" if ps else ns


def _fac_slug(fac_id, provider, name) -> str:
    """Canonical facility slug for EMITTED links (comparables, 301 targets).

    r-routeslug (2026-07-31): DELEGATES to the freeze composer
    (facility_slug_freeze.build_canonical_slug — provider-prefix dedupe +
    ascii folding), byte-identical to the sitemap/freeze. The old local
    compose emitted the doubled pre-dedupe form for unfrozen brand-prefixed
    rows. The hash8 tail is unchanged (keyed on STABLE provider|name,
    r-stable-slug 2026-06-16), so emitted links resolve exactly as before;
    fac_id stays in the signature for callers but the hash never keys on it.
    Returns "" when un-sluggable (short/empty name) — callers skip those
    rather than emit a broken link."""
    from routes.facility_slug_freeze import build_canonical_slug
    return build_canonical_slug(provider, name) or ""


# Legal-suffix / filler tokens dropped from legacy-slug matching.
_SLUG_STOPWORDS = {"inc", "llc", "corp", "ltd", "plc", "co", "sa", "ag",
                   "gmbh", "the", "group", "holdings", "company"}


def _resolve_legacy_slug(slug: str):
    """Old indexed slugs embed MD5(id)[:8]; re-ingestion assigns NEW ids (and
    creates duplicate rows), so previously-indexed facility slugs 404 even though
    the facility still exists under a new slug — silently de-indexing ~14K pages.

    Recover the SEO equity: match the slug's name-part to the live row(s) and
    return the CURRENT canonical slug for a 301. HIGH PRECISION — only an exact
    canonical name-part match (handles pure hash-churn + duplicates) or a single
    unique token match (handles cleaned company prefixes) wins. Anything
    ambiguous returns None and falls through to the existing 404, so we never
    mis-redirect. Validated at 95% recovery / 0 mis-redirects on the GSC export.
    """
    base = slug[:-5] if slug.endswith(".html") else slug
    base = base.split("/")[0]
    parts = base.rsplit("-", 1)
    is_hash = (len(parts) == 2 and len(parts[1]) == 8
               and all(ch in "0123456789abcdef" for ch in parts[1].lower()))
    name_part = parts[0] if is_hash else base
    # Distinctive tokens for the DB pre-filter. Drop legal suffixes + BARE
    # numbers ('1'/'3' would LIKE-match everything); the number is still
    # enforced by the exact name-part comparison below.
    toks = [t for t in dict.fromkeys(name_part.split("-"))
            if t and t not in _SLUG_STOPWORDS and not t.isdigit()]
    if len(toks) < 2:
        return None
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return None
        try:
            c = conn.cursor()
            conds = " AND ".join(
                ["LOWER(COALESCE(provider,'')||' '||COALESCE(name,'')) LIKE %s"] * len(toks))
            # Stored-first 301 target: probe for the frozen column (live DDL
            # can lag) so a confident match lands on the LIVE canonical slug —
            # for pre-dedupe-frozen rows that is the stored doubled form, NOT
            # today's builder output (the freeze is forward-only).
            _has_canon = False
            try:
                c.execute("SELECT 1 FROM information_schema.columns "
                          "WHERE table_name='discovered_facilities' "
                          "AND column_name='canonical_slug'")
                _has_canon = c.fetchone() is not None
            except Exception:
                try: conn.rollback()
                except Exception: pass
            _cs = "canonical_slug" if _has_canon else "NULL AS canonical_slug"
            c.execute(
                "SELECT id, provider, name, " + _cs + " FROM discovered_facilities "
                "WHERE name IS NOT NULL AND name <> '' AND " + conds + " "
                "ORDER BY COALESCE(power_mw,0) DESC, id ASC LIMIT 25",
                [f"%{t}%" for t in toks])
            cands = c.fetchall()
            if not cands:
                return None
            # GUARD: if the distinctive tokens match many facilities, this is a
            # GENERIC name (provider repeated as the name, e.g. "amazon-web-
            # services-amazon-web-services" → 166 distinct sites). Not safely
            # resolvable to one facility — bail rather than redirect to the wrong
            # one. Specific names match only the facility + its few duplicates.
            if len(cands) >= 25:
                return None
            # Tier 0: exact name-part match against the PRE-dedupe compose
            # (_legacy_name_part) — legacy indexed slugs were built with the
            # unconditional provider prefix, so the matcher must reproduce
            # THAT form, not today's deduped one. Catches pure hash-churn +
            # real duplicates. GUARD: generic names (provider repeated as
            # name, e.g. "amazon-web-services-amazon-web-services") map to
            # dozens of DISTINCT facilities sharing one name-part; redirecting
            # those would point the old URL at the WRONG facility, so only
            # resolve a SMALL match set.
            exact = [row for row in cands
                     if _legacy_name_part(row[1], row[2]) == name_part]
            if 1 <= len(exact) <= 3:
                rid, rprov, rname, rcanon = exact[0]   # best (power) first
                return (rcanon or _fac_slug(rid, rprov, rname)) or None
            if len(exact) > 3:
                return None                     # generic name → not safely resolvable
            # Tier 1: exactly one candidate carries every distinctive token
            # (covers cleaned company prefixes where the name-part changed).
            if len(cands) == 1:
                rid, rprov, rname, rcanon = cands[0]
                return (rcanon or _fac_slug(rid, rprov, rname)) or None
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"legacy slug resolve failed: {e}")
        return None


def _comparables_html(fac: dict, limit: int = 6) -> str:
    """Internal-link mesh: other data centers in the same city/market. Adds
    unique per-page content + crawl depth — the core of turning thin facility
    pages into indexable ones. Links use the canonical slug (no dup URLs)."""
    city  = (fac.get("city") or "").strip()
    state = (fac.get("state") or "").strip()
    country = (fac.get("country") or "").strip()
    fid   = fac.get("id")
    # ★ 2026-08-21: discovered_facilities.id is INTEGER, but the page is ALSO
    # rendered for rows from the `facilities` table, whose ids are TEXT slugs
    # ('meta-rosemount-mn', 'osm_64e7c52ab686d633', …). Binding one of those
    # into `WHERE id <> %s` made Postgres raise
    #   invalid input syntax for type integer: "meta-rosemount-mn"
    # on EVERY such page — the fail-soft below returned "" and the one block
    # of unique content this module exists to provide rendered empty, silently,
    # dozens of times a minute in the Railway log. A non-integer id cannot be a
    # discovered_facilities row, so there is nothing to exclude: bind -1.
    try:
        fid = int(fid)
    except (TypeError, ValueError):
        fid = -1
    if not (city or state):
        return ""
    # 'Regional' is a PLACEHOLDER this dataset uses when the real city is
    # unknown — 314 rows across 30 countries carry it. Same-country matching
    # (below) already stops it linking across continents, but two facilities
    # being jointly unlocated is not a reason to call them neighbours, so the
    # city branch does not fire on it. state still can, which is the honest
    # weaker signal. Measured 2026-08-14; 'California Regional' and
    # 'Connecticut Regional' (136 rows each) are REAL market labels, not this
    # placeholder, and are deliberately not matched here.
    if city.lower() in ("regional", "unknown", "n/a", "none", "other"):
        city = ""
        if not state:
            return ""
    rows = []
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return ""
        try:
            c = conn.cursor()
            # r-slug-freeze (2026-07-03): link to the FROZEN canonical_slug so
            # the internal-link mesh can't drift into duplicate URLs after a
            # post-freeze name change. Probe the column; degrade to live-compute.
            _has_canon = False
            try:
                c.execute("SELECT 1 FROM information_schema.columns "
                          "WHERE table_name='discovered_facilities' "
                          "AND column_name='canonical_slug'")
                _has_canon = c.fetchone() is not None
            except Exception:
                try: conn.rollback()
                except Exception: pass
            _cs = "canonical_slug" if _has_canon else "NULL AS canonical_slug"
            # ★★★ COUNTRY IS PART OF THE MATCH (2026-08-14). It was not, and
            # "nearby" was a bare string compare on city/state, so a city name
            # that exists in more than one country linked across continents.
            # Measured live on this table the same day:
            #
            #   city 'Regional' (a PLACEHOLDER, not a place) — 314 facilities
            #        across 30 countries, all mutually "nearby"
            #   'San Juan' spans 4 countries; London / Dublin / Santiago /
            #        Vienna / Manchester / San Jose / Barcelona / Rome /
            #        Richmond each span 3
            #
            #   16,426 pages render this module
            #    2,004 of them (12.2%) showed >= 1 wrong-country neighbour
            #    7,144 of 91,409 rendered links (7.8%) pointed to another country
            #
            # Worst single group: London GB, 236 pages. A Romanian facility was
            # offering Mexican, Brazilian and Lithuanian data centers as
            # "comparable facilities in Regional".
            #
            # ★ This is the module whose whole job is to make a thin facility
            # page worth indexing. Filling it with cross-continent links makes
            # the page WORSE than empty: it is the one block of unique content,
            # and it was wrong on one page in eight.
            #
            # NULL/'' country matches only other NULL/'' country rows, rather
            # than matching everything. A row that does not know where it is
            # must not claim to be near anything — the alternative (treat
            # unknown as a wildcard) is how 'Regional' became global.
            c.execute("""
                SELECT id, name, provider, power_mw, """ + _cs + """
                  FROM discovered_facilities
                 WHERE id <> %s
                   AND name IS NOT NULL AND name <> ''
                   AND LOWER(COALESCE(country, '')) = LOWER(%s)
                   AND ( (%s <> '' AND LOWER(city)  = LOWER(%s))
                      OR (%s <> '' AND LOWER(state) = LOWER(%s)) )
                 ORDER BY (CASE WHEN %s <> '' AND LOWER(city) = LOWER(%s) THEN 0 ELSE 1 END),
                          COALESCE(power_mw, 0) DESC
                 LIMIT %s
            """, (fid, country, city, city, state, state, city, city, limit))
            rows = c.fetchall()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"facility_profile comparables failed: {e}")
        return ""
    if not rows:
        return ""
    items = []
    for rid, rname, rprov, rpow, rcanon in rows:
        slug = rcanon or _fac_slug(rid, rprov, rname)
        if not slug:      # un-sluggable (sub-3-char name) — skip, don't 404-link
            continue
        extra = ""
        if rprov and rprov.strip().lower() != (rname or "").strip().lower():
            extra += f" &middot; {_esc(rprov)}"
        if rpow and str(rpow) not in ("0", "0.0"):
            extra += f" &middot; {_esc(rpow)} MW"
        items.append(
            f'<li style="margin:4px 0"><a class="link" href="/facilities/{_esc(slug)}">{_esc(rname)}</a>'
            f'<span style="opacity:.65;font-size:13px">{extra}</span></li>'
        )
    where = city or state or "the area"
    return (
        '<div class="section"><div class="section-head"><h2>Other data centers nearby</h2></div>'
        f'<p class="section-sub">Comparable facilities in {_esc(where)} tracked by DC Hub &mdash; '
        'compare power, operators, and grid context across the market.</p>'
        f'<ul style="margin:10px 0 0;padding-left:18px">{"".join(items)}</ul></div>'
    )


def _narrative(fac: dict, dcpi) -> str:
    """Unique, data-grounded intro paragraph — turns a thin templated page into
    indexable unique prose (every sentence sourced from this facility's data)."""
    name = fac.get("name") or "This data center"
    provider = (fac.get("provider") or "").strip()
    city, state, country = (fac.get("city") or ""), (fac.get("state") or ""), (fac.get("country") or "")
    power = fac.get("power_mw")
    status = (fac.get("status") or "").strip()
    loc = ", ".join([p for p in (city, state, country) if p])
    bits = []
    lead = f"<strong>{_esc(name)}</strong> is a data center"
    if provider and provider.lower() != (name or "").strip().lower():
        lead += f" operated by {_esc(provider)}"
    if loc:
        lead += f" in {_esc(loc)}"
    bits.append(lead + ".")
    if power and str(power) not in ("0", "0.0"):
        s = f"It carries a reported power capacity of {_esc(power)} MW"
        if status and status.lower() != "unknown":
            s += f" and is currently {_esc(status.lower())}"
        bits.append(s + ".")
    elif status and status.lower() != "unknown":
        bits.append(f"The facility is currently {_esc(status.lower())}.")
    if dcpi:
        v = (dcpi.get("verdict") or "").upper()
        mname = dcpi.get("market_name") or state or "its regional"
        iso = dcpi.get("iso") if _is_registered_label(dcpi.get("iso")) else ""
        ttp = dcpi.get("time_to_power_months")
        s = f"It sits in the {_esc(mname)} data-center market"
        if iso:
            s += f" on the {_esc(iso)} grid"
        s += ", which DC Hub's Data Center Power Index"
        s += f" currently rates <strong>{_esc(v)}</strong>" if v else " scores"
        if ttp is not None:
            s += f", with an estimated {_esc(ttp)}-month time-to-power"
        bits.append(s + ".")
        interp = {
            "BUILD":   "For operators evaluating new capacity, this market screens favorably — grid headroom and interconnection timelines are comparatively strong.",
            "AVOID":   "Operators should weigh interconnection risk here — the market screens constrained on grid headroom and time-to-power.",
            "CAUTION": "The market shows mixed signals — validate live grid headroom and queue depth before committing capacity.",
        }.get(v)
        if interp:
            bits.append(interp)
    return ('<p class="section-sub" style="font-size:15.5px;line-height:1.75;'
            'margin:6px 0 18px;max-width:720px">' + " ".join(bits) + "</p>")


def _market_context_html(mslug: str, mname: str) -> str:
    """r-soft404-rag (2026-07-06): bare facility pages are Google soft-404s (thin,
    ~82 words). When the facility's market has a RAG-generated deep-dive narrative
    (market_deep_dives.narrative_md), splice its first ~2 paragraphs in — with a
    citable /dcpi link — so the page carries SUBSTANTIVE, UNIQUE, indexable content
    instead of bare metadata. Fail-soft: '' on any miss/thin narrative; Redis-cached
    read (no self-request); auto-covers a market the moment its deep-dive backfills."""
    if not mslug:
        return ""
    try:
        from routes.market_deep_dive import read_deep_dive
        import re as _re
        dd = read_deep_dive(mslug)
        # r-nova-zero (2026-08-01): a brief written from facility_count=0
        # facts is a data bug wearing prose ("avoid entering Northern
        # Virginia" shipped off a dead join) — never splice one into a
        # facility page. Deliberately narrower than the /markets render
        # guard: a null SCORE never reaches this prose, so score-only-broken
        # briefs keep covering thin pages while the backfill runs.
        if not ((dd or {}).get("key_stats") or {}).get("facility_count"):
            return ""
        md = ((dd or {}).get("narrative_md") or "").strip()
        if len(md) < 200:
            return ""
        paras = [p.strip() for p in md.split("\n\n")
                 if p.strip() and not p.strip().lstrip().startswith("#")]
        snippet = " ".join(paras[:2])
        snippet = _re.sub(r'[*_`>#\[\]()]+', '', snippet).strip()
        if len(snippet) > 900:
            snippet = snippet[:900].rsplit(" ", 1)[0] + "…"
        if len(snippet) < 140:
            return ""
        return (
            '<div class="section"><div class="section-head"><h2>Market context</h2></div>'
            f'<p class="section-sub">DC Hub analyst read on {_esc(mname)} &mdash; the market '
            f'this facility sits in (<a href="/dcpi/{_esc(mslug)}" class="link">full deep-dive '
            f'&rarr;</a>).</p><p>{_esc(snippet)}</p></div>'
        )
    except Exception:
        return ""


def _brand_already_in_name(provider: str, name: str) -> bool:
    """True when prepending `provider` to `name` would double the brand in the
    SERP title — measured 2026-08-01 as a corpus-wide CTR drag: "DataBank
    DataBank Dallas (DFW2)", "Vantage Data Centers Vantage Berlin II", "Oso
    Grande Technologies, Inc. Oso Grande Technologies". Three cases: provider
    inside name (the old check), name inside provider (legal-suffix operator
    strings), and a shared leading brand word ("Vantage …" vs "Vantage Data
    Centers"). Titles/desc/h1 only — the FROZEN slug is composed elsewhere and
    is never touched here."""
    import re as _re
    p = (provider or "").lower().strip()
    n = (name or "").lower().strip()
    if not p or not n:
        return False
    if p in n or n in p:
        return True
    pt = _re.findall(r"[a-z0-9]+", p)
    nt = _re.findall(r"[a-z0-9]+", n)
    # Leading-word brand match. Generic first words are not a brand signal —
    # "Data Foundry" vs a name starting "Data Center …" must still prepend.
    _generic = {"the", "data", "center", "centre", "datacenter", "datacenters",
                "dc", "global"}
    return bool(pt and nt and pt[0] == nt[0] and pt[0] not in _generic)


# r-junk-noindex (2026-08-01): nameless-OSM junk ("Data Center 343593591 —
# West Chicago", bare 6+ digit names, unknown-osm-dc-<id> frozen slugs) can
# never rank; indexed junk titles drag quality scoring for the whole corpus.
# The page keeps serving 200 — it just asks not to be indexed.
_JUNK_NAME_RE = None
_JUNK_SLUG_RE = None


def _is_osm_junk(name, slug) -> bool:
    global _JUNK_NAME_RE, _JUNK_SLUG_RE
    import re as _re
    if _JUNK_NAME_RE is None:
        _JUNK_NAME_RE = _re.compile(
            r"(?i)^\s*(?:data\s+cent(?:er|re)|osm\s+dc)\s*#?\d{6,}\b")
        _JUNK_SLUG_RE = _re.compile(r"(?:^|-)data-center-\d{6,}(?:-|$)")
    n = (name or "").strip()
    s = slug or ""
    return bool(_JUNK_NAME_RE.match(n)
                or (n.isdigit() and len(n) >= 6)
                or s.startswith("unknown-osm-")
                or _JUNK_SLUG_RE.search(s))


def _is_junk_facility(name, slug) -> bool:
    """Every class of page that serves 200 but must not be indexed.

    r-headline-noindex (2026-08-09) adds the class _is_osm_junk cannot see:
    news headlines and NER spans ingested as facility names — "Stack breaks
    ground on second Tokyo data center", "$1.2 billion data center breaks
    ground in Cheyenne … - Oil City News", "Meta Unknown". Slugs are FROZEN,
    so these pages keep serving 200 under their existing URL; they just stop
    asking to be indexed. Name-shape ONLY — never the evidence test, which
    would also de-index 45 real coordinate-less OSM facilities. See
    util/facility_name_sanity.py for the calibration.

    r-ner-noindex (2026-08-09) adds the class the NAME cannot see either:
    61 already-published single-token NER spans — "Copilot", "FERC",
    "GitHub", "Intel". Nothing about those strings distinguishes them from a
    real single-word operator, so the discriminator is PROVENANCE, resolved
    once in SQL into a slug set (util/facility_ner_noindex.py) rather than
    re-derived per row here. The set is empty until something refreshes it,
    which keeps this function DB-free for _render_profile's callers.

    r-news-pipeline-noindex (2026-08-09) takes that set 62 → 91: the OLDER
    news path (source='news_pipeline') published article TITLES as
    facilities, and 3 escaped the name predicate above by one word each
    ("announce" is not a listed verb; "Urban Milwaukee" / "Bridge Michigan"
    / "JLL" are not listed publications). ★ Those are suppressed by the slug
    set, NOT by name — that source also holds 30 REAL facilities (Stargate
    Abilene, NTT Frankfurt), which is why its prong is evidence-fenced.

    ★ `slug` MUST be the FROZEN canonical_slug, not the request slug. Two of
    the three arms below are slug-keyed (the NER/news set entirely; two of
    _is_osm_junk's four), so passing the arrival slug silently unsuppresses
    every hash8 alias — which is exactly what happened until PR #2501
    (2026-08-09) put `canonical_slug` in _fetch_facility_by_slug's column
    lists. Its one production caller, _render_profile, passes `_fslug`.
    """
    if _is_osm_junk(name, slug):
        return True
    try:
        from util.facility_ner_noindex import is_suppressed_slug
        if is_suppressed_slug(slug):
            return True
    except Exception:
        pass
    try:
        from util.facility_name_sanity import headline_reject_reason
        return bool(headline_reject_reason(name))
    except Exception:
        return False


def _render_profile(fac: dict, slug: str) -> str:
    """Server-rendered facility profile. Matches the static file
    visual style so transitions between static + dynamic are seamless."""
    name = fac.get("name") or "Data Center"
    provider = fac.get("provider") or "Operator"
    city = fac.get("city") or ""
    state = fac.get("state") or ""
    country = fac.get("country") or ""
    region = fac.get("region") or ""
    power = fac.get("power_mw")
    status = fac.get("status") or "Unknown"
    address = fac.get("address") or ""
    lat = fac.get("latitude")
    lng = fac.get("longitude")

    loc_short = ", ".join([p for p in (city, state, country) if p])
    # r-geo-facility-title (2026-06-24): rich, entity-bearing title/desc/h1 instead
    # of city-only "{name} | DC Hub". The on-demand renderer serves ~90% of facility
    # pages (only ~2,002 have static files), and a city-only title (a) drops the
    # OPERATOR — the strongest signal an AI crawler uses to identify+cite a facility —
    # and (b) duplicates across every facility in a city. Prepend the operator unless
    # the brand is already in the name (substring EITHER way, or shared leading
    # brand word — the plain `provider in name` check shipped SERP titles like
    # "Vantage Data Centers Vantage Berlin II"; see _brand_already_in_name).
    _op = "" if (not provider or provider == "Operator" or _brand_already_in_name(provider, name)) else f"{provider} "
    _disp = f"{_op}{name}".strip()
    title = (f"{_disp} — {loc_short} Data Center | DC Hub" if loc_short
             else f"{_disp} Data Center | DC Hub")
    desc = (f"{_disp} is a data center"
            f"{f' operated by {provider}' if _op else ''}"
            f"{f' in {loc_short}' if loc_short else ''}. "
            f"{f'Power capacity: {power} MW. ' if power else ''}"
            f"View specs, location, power & connectivity on DC Hub.")

    # r-frozen-slug (2026-07-06): canonicalize to the facility's FROZEN slug (the
    # one the sitemap + the /facility 301 both use), not whatever slug the request
    # arrived on — else a page reached via an alias/legacy slug declares ITSELF
    # canonical, splitting Google's index signals (GSC alternate/canonical churn).
    _fslug = fac.get("canonical_slug") or slug
    canonical = f"https://dchub.cloud/facilities/{_fslug}"

    # r-junk-noindex (2026-08-01): numeric-OSM junk pages serve 200 but ask
    # not to be indexed — junk "Data Center <10-digit-id>" titles were indexed
    # and dragging corpus-wide quality/CTR (08-01 diagnosis).
    # LANE 3 (thin-content program, 2026-08-14): _is_junk_facility is
    # name/slug shaped BY DESIGN and DB-free, so it cannot see a page that is
    # perfectly well-named and simply has nothing to say. 408 of 17,948 live
    # facilities carry no power, no coordinates, no address and no real city;
    # the page renders Status/Country and one sentence. Google already refuses
    # to index them ("Crawled – currently not indexed"), so asking is spending
    # crawl budget that a rankable page could have had.
    #
    # ★ ALL FOUR facts must be absent — see util/thin_content.is_contentless.
    # An evidence test on coordinates alone would de-index 45 REAL
    # coordinate-less OSM facilities, which is exactly why _is_junk_facility
    # never took one. noindex is NOT deletion: the page keeps serving 200 at
    # its frozen slug.
    from util.thin_content import is_contentless as _contentless
    _robots = ("noindex" if (_is_junk_facility(name, _fslug) or _contentless(fac))
               else "index, follow")

    # ★★ 2026-07-28 — a KNOWN duplicate must canonicalise to its TWIN, not itself.
    # We flag 7,928 facilities is_duplicate and then served every one of them a
    # SELF-canonical, so two byte-identical pages each declared itself the
    # original. That is the textbook way to land in "Crawled - currently not
    # indexed" (4,494) and "Duplicate without user-selected canonical" (1,492):
    # Google sees the pair, cannot pick, and indexes neither. Verified on live
    # pairs — two /facilities/ URLs whose rendered text is 100.0% identical.
    # Pointing at the surviving row consolidates the signals instead of splitting
    # them. 6,192 flagged rows carry a duplicate_of_id; 5,674 of those resolve to
    # a live page, and only a RESOLVED twin is used — an unresolvable pointer
    # leaves the self-canonical alone rather than inventing a target.
    # ★★ 2026-07-28 (third pass): consolidate on duplicate_of_id ALONE, not on
    # is_duplicate. Setting is_duplicate=1 is a VISIBILITY flag — it drops the
    # row from every is_duplicate-filtered count and from the sitemap, which is
    # exactly how 9,318 facilities went missing and why
    # repair_dedup_keeper_election.py had to elect keepers on 2026-07-27. I
    # reproduced that bug at small scale: 57 of 58 slugs I flagged were left
    # with no keeper, and it was reverted.
    # The SEO job needs none of that. A duplicate_of_id pointer is enough to
    # emit rel=canonical at the twin: the row stays live, stays counted, keeps
    # serving 200, and Google consolidates the two URLs on its own. Suppression
    # deletes a page; a canonical MERGES it.
    try:
        if fac.get("duplicate_of_id"):
            _twin = _canonical_twin_url(fac.get("duplicate_of_id"))
            if _twin and _twin != canonical:
                canonical = _twin
    except Exception:
        pass

    # Schema.org JSON-LD
    import json as _json
    # 2026-06-29: enrich for AI-answer-engine citation (Copilot already cites these
    # pages). Add @id/url/identifier/license + drop null address keys (was emitting
    # literal null). Only data already on the page — no fabricated fields.
    _hash8 = slug.rsplit("-", 1)[-1] if "-" in slug else slug
    _addr = {k: v for k, v in {
        "@type": "PostalAddress",
        "streetAddress": address or None,
        "addressLocality": city or None,
        "addressRegion": state or None,
        "addressCountry": country or None,
    }.items() if v is not None}
    schema = {
        "@context": "https://schema.org",
        "@type": "Place",
        "@id": canonical + "#place",
        "url": canonical,
        "name": _disp,
        "description": f"Data center facility operated by {provider} in {loc_short}. Source: DC Hub (dchub.cloud), CC-BY-4.0.",
        "identifier": _hash8,
        "isAccessibleForFree": True,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "address": _addr,
        "additionalType": "https://schema.org/DataCenter",
    }
    if lat and lng:
        schema["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": float(lat),
            "longitude": float(lng),
        }
    # Dataset node = the explicit "you may cite/reproduce this" signal AI engines
    # look for (CC-BY-4.0, DC Hub as creator). Additive; references the Place @id.
    dataset_ld = _json.dumps({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "@id": canonical + "#dataset",
        "name": f"{_disp} — facility intelligence record",
        "description": f"Structured data-center facility record (location, operator, power & connectivity context) for {_disp}.",
        "url": canonical,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "spatialCoverage": {"@id": canonical + "#place"},
        # r-page-onramp (2026-07-04): crawl->tool crossover. Point the Dataset
        # at the LIVE query surfaces so an agent that lands on the crawled page
        # can jump straight to querying: distribution -> the MCP endpoint,
        # potentialAction -> the keyless RAG search (routes/brain_rag.py).
        "distribution": [{
            "@type": "DataDownload",
            "encodingFormat": "application/json",
            "contentUrl": "https://dchub.cloud/mcp",
        }],
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": "https://dchub.cloud/api/v1/rag/search?q={search_term_string}",
            },
            "query-input": "required name=search_term_string",
        },
    }, indent=2)

    # Enriched stat cards — only render values we actually have (sparse rows
    # with power_mw=0 / blank fields used to render a wall of empties).
    def _has(v):
        return v not in (None, "", 0, 0.0, "0", "Unknown", "unknown")
    stats = []
    if _has(power):                    stats.append(("Power", f"{power} MW"))
    if _has(status):                   stats.append(("Status", str(status).title()))
    if _has(region):                   stats.append(("Market", region))
    if _has(city) and city != region:  stats.append(("City", city))
    if _has(state):                    stats.append(("State", state))
    if _has(country):                  stats.append(("Country", country))
    if lat and lng:                    stats.append(("Coordinates", f"{float(lat):.4f}, {float(lng):.4f}"))
    if _has(address):                  stats.append(("Address", address))
    # r82 (2026-06-30): Clarity showed DEAD CLICKS on the metric tiles — they're
    # styled like buttons but were plain <div>s. Make the tap-worthy ones real
    # links (Market → its DCPI page, Coordinates → the map). Doubles as onward-nav
    # (lifts the ~1.19 pages/session). _dcpi is fetched here (moved up) so the
    # Market tile can point at the same guaranteed-resolving /dcpi/<slug>.
    _dcpi = _market_dcpi(city, state, lat, lng)
    _mslug0 = (_dcpi.get("market_slug") or "") if _dcpi else ""
    _osm_href = (f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}#map=14/{lat}/{lng}"
                 if (lat and lng) else "")

    # r84 (2026-07-04): Clarity STILL showed dead clicks on the stat grid — r82
    # only linked Market+Coordinates (and Market silently died when the market
    # didn't resolve to a DCPI slug, the common intl case), leaving Power/Status/
    # City/State/Country as button-styled dead <div>s. Give the two highest-intent
    # tiles real destinations: Power → the /sites capacity report (the number users
    # jab at), Country → the country hub (only when it's an ISO2 code, so we never
    # mint a spaces-in-URL 404). Link tiles also carry a ↗ affordance (CSS) so the
    # clickable ones read as clickable and the remaining static tiles read as static.
    _cc = (country or "").strip().lower()
    _tile_href = {
        "Power":       f"/sites/{_esc(slug)}" if slug else "",
        "Country":     f"/facilities/in/{_esc(_cc)}" if (len(_cc) == 2 and _cc.isalpha()) else "",
        "Market":      f"/dcpi/{_esc(_mslug0)}" if _mslug0 else "",
        "Coordinates": _osm_href,
    }

    def _tile(label, value):
        _in = (f'<div class="stat-label">{_esc(label)}</div>'
               f'<div class="stat-value">{_esc(value)}</div>')
        _href = _tile_href.get(label, "")
        if _href:
            if _href.startswith("http"):
                _t = ' target="_blank" rel="noopener"'
            elif _href.startswith("/sites/"):
                # /sites/<slug> is robots-blocked (an unbounded identical-shell
                # crawl sink); don't leak link equity into a page we tell crawlers
                # not to fetch.
                _t = ' rel="nofollow"'
            else:
                _t = ""
            return f'<a class="stat-card stat-link" href="{_href}"{_t}>{_in}</a>'
        return f'<div class="stat-card">{_in}</div>'
    stats_html = "".join(_tile(label, value) for label, value in stats)

    # DCPI market-intelligence block (best-effort — this is an intelligence
    # platform, so a facility page should carry its market's DCPI verdict).
    dcpi_html = ""
    _mkt_crumb = ""   # r81: facility→hub breadcrumb (completes the SEO mesh —
                      # r80 added hub→facility links; this is the return half)
    if _dcpi:
        _verdict = (_dcpi.get("verdict") or "").upper()
        _vcolor = "#10b981" if _verdict == "BUILD" else ("#ef4444" if _verdict == "AVOID" else "#f59e0b")
        _mslug = _dcpi.get("market_slug") or ""
        _mname = _dcpi.get("market_name") or region or "this market"
        # r-geo-headers (2026-07-30, Gemini GEO review): the ISO is a first-class
        # retrieval key — surface it in <title> so "<ISO> data center" queries and
        # AI retrievers key on the grid, not just the city. Guarded on real DCPI
        # data; never inferred here (iso_defaults fails open — must not reach a title).
        # r-iso-unk (2026-08-27): 'UNK' is the not-a-label sentinel, not an
        # operator — a bare truthy test put "UNK grid" in the <title> of every
        # page resolving to barueri/bologna/midrand/osasco.
        if _is_registered_label(_dcpi.get("iso")):
            title = title.replace(" | DC Hub", f" | {_dcpi['iso']} grid | DC Hub")
        _chips = []
        if _is_registered_label(_dcpi.get("iso")):         _chips.append(("ISO", _esc(_dcpi.get("iso"))))
        if _dcpi.get("excess_power_score") is not None:   _chips.append(("Excess-power", _esc(_dcpi.get("excess_power_score"))))
        if _dcpi.get("constraint_score") is not None:     _chips.append(("Constraint", _esc(_dcpi.get("constraint_score"))))
        if _dcpi.get("time_to_power_months") is not None: _chips.append(("Time-to-power", f'{_esc(_dcpi.get("time_to_power_months"))} mo'))
        # r82: chips + verdict pill were dead clicks (button-styled, no handler).
        # Link them to the same guaranteed /dcpi/<slug> as the breakdown link.
        _chip_open = (f'<a href="/dcpi/{_esc(_mslug)}" class="chip chip-link">'
                      if _mslug else '<div class="chip">')
        _chip_close = '</a>' if _mslug else '</div>'
        _chips_html = "".join(
            f'{_chip_open}<span class="chip-l">{l}</span><span class="chip-v">{v}</span>{_chip_close}'
            for l, v in _chips)
        _dlink = f'<a href="/dcpi/{_esc(_mslug)}" class="link">Full DCPI breakdown &rarr;</a>' if _mslug else ""
        # /dcpi/<mslug> is guaranteed to resolve (same slug the DCPI page keys
        # on) and now carries this market's facility list — safer than /markets
        # which uses inverse metro slugs (the slug-convention trap).
        if _mslug:
            _mkt_crumb = f'<a href="/dcpi/{_esc(_mslug)}">{_esc(_mname)}</a> · '
        _verdict_pill = f'<span class="verdict" style="color:{_vcolor};border-color:{_vcolor}">{_esc(_verdict or "NEUTRAL")}</span>'
        if _mslug:
            _verdict_pill = f'<a href="/dcpi/{_esc(_mslug)}" class="verdict-link" title="Full DCPI breakdown">{_verdict_pill}</a>'
        dcpi_html = (
            '<div class="section"><div class="section-head">'
            '<h2>Market intelligence</h2>'
            f'{_verdict_pill}'
            '</div>'
            f'<p class="section-sub">Data Center Power Index verdict for {_esc(_mname)} &mdash; the market this facility sits in.</p>'
            f'<div class="chips">{_chips_html}</div>{_dlink}</div>'
        )

    narrative_html = _narrative(fac, _dcpi)
    comps_html = _comparables_html(fac)
    # LANE 2 (+ LANE 1 when THIN_INFRA_SLICE=1): render the market/ISO/DCPI
    # facts this page already had access to and was throwing away. Returns ''
    # when there is nothing true to add, so a LANE-3 page does not gain an
    # empty header.
    try:
        from util.thin_content import context_block as _ctx_block
        context_html = _ctx_block(fac, _dcpi)
    except Exception as _ctx_err:
        logger.warning(f"facility_profile context block failed: {_ctx_err}")
        context_html = ""
    # r-soft404-rag: RAG market-narrative snippet — turns a thin facility page into
    # substantive, indexable content when its market has a deep-dive (fail-soft '').
    _mkt_context_html = _market_context_html(
        _mslug0, ((_dcpi.get("market_name") if _dcpi else "") or region or "this market"))

    # P1-1 (2026-08-28): Product 1's sponsored module. Returns '' whenever no
    # sponsor is active, which is its state until a row is activated — so this
    # is inert on every page today. This route is the one that actually serves
    # /facilities/<slug> (x-dc-hub-source: facility-profile-dynamic-backend);
    # the ~2,000 static files under dchub-frontend/facilities/ are shadowed and
    # rendering into them would put the module nowhere.
    try:
        from routes.sponsor_render import sponsor_module_html
        sponsor_html = sponsor_module_html("facility_module")
    except Exception as _sp_err:
        logger.warning(f"facility_profile sponsor module failed: {_sp_err}")
        sponsor_html = ""

    map_block = ""
    if lat and lng:
        # Cheap inline map preview via OpenStreetMap static tile
        bbox = f"{float(lng)-0.05},{float(lat)-0.04},{float(lng)+0.05},{float(lat)+0.04}"
        # r82: the static map iframe (scrolling=no) ate every tap → dead click.
        # Overlay a full-size transparent link so tapping the map opens OSM.
        map_block = f"""
        <div class="section">
          <div class="section-head"><h2>Location</h2></div>
          <div style="position:relative;margin-top:8px">
            <iframe width="100%" height="320" frameborder="0" scrolling="no" loading="lazy"
              marginheight="0" marginwidth="0" title="Facility location map"
              src="https://www.openstreetmap.org/export/embed.html?bbox={bbox}&layer=mapnik&marker={lat},{lng}"
              style="border:1px solid var(--b);border-radius:12px;display:block"></iframe>
            <a href="{_osm_href}" target="_blank" rel="noopener"
               aria-label="Open location in OpenStreetMap"
               style="position:absolute;inset:0;z-index:2;border-radius:12px"></a>
          </div>
          <p style="margin-top:10px">
            <a href="{_osm_href}"
               target="_blank" class="link" style="margin-top:0">Open in OpenStreetMap &rarr;</a>
          </p>
        </div>
        """

    # r83 SEO: BreadcrumbList JSON-LD (the r81 breadcrumb was visual-only).
    # Marking it up earns a rich breadcrumb trail in Google AND Bing results
    # and reinforces the facility↔market mesh. Home › <market hub> › <facility>.
    _crumb_items = [{"@type": "ListItem", "position": 1, "name": "Home",
                     "item": "https://dchub.cloud/"},
                    {"@type": "ListItem", "position": 2, "name": "Facilities",
                     "item": "https://dchub.cloud/facilities"}]
    _pos = 3
    if country:
        _crumb_items.append({"@type": "ListItem", "position": _pos, "name": country,
                             "item": f"https://dchub.cloud/facilities/in/{country.lower().strip()}"})
        _pos += 1
    if _mkt_crumb and _mslug:
        _crumb_items.append({"@type": "ListItem", "position": _pos,
                             "name": _mname,
                             "item": build_public_url("dcpi", _mslug)})
        _pos += 1
    _crumb_items.append({"@type": "ListItem", "position": _pos,
                         "name": name, "item": canonical})
    # Visual breadcrumb crumb for the country hub (the /facilities + per-country
    # mesh — un-orphans this page; the on-demand renderer is ~90% of facility pages).
    _country_crumb = (f'<a href="/facilities/in/{_esc(country.lower().strip())}">{_esc(country)}</a> · '
                      if country else '')
    _breadcrumb_ld = _json.dumps({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": _crumb_items}, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(desc)}">
<meta name="robots" content="{_robots}">
<link rel="canonical" href="{_esc(canonical)}">
<meta property="og:title" content="{_esc(_disp)} — Data Center">
<meta property="og:description" content="{_esc(desc)}">
<meta property="og:type" content="place">
<meta property="og:url" content="{_esc(canonical)}">
<meta property="og:site_name" content="DC Hub">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{_esc(name)}">
<meta name="twitter:description" content="{_esc(desc[:200])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script type="application/ld+json">{_json.dumps(schema, indent=2)}</script>
<script type="application/ld+json">{dataset_ld}</script>
<script type="application/ld+json">{_breadcrumb_ld}</script>
<style>
  :root{{--bg:#0a0a0f;--surf:#131319;--surf2:#1a1a22;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--indd:#6366f1;--vio:#a855f7;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--tx);line-height:1.6;-webkit-font-smoothing:antialiased}}
  .header{{border-bottom:1px solid var(--b);padding:16px 0;position:sticky;top:0;background:rgba(10,10,15,0.85);backdrop-filter:blur(10px);z-index:10}}
  .header-inner,.container,.breadcrumb{{max-width:1080px;margin:0 auto;padding:0 24px}}
  .header-inner{{display:flex;justify-content:space-between;align-items:center}}
  .logo{{font-size:21px;font-weight:700;color:var(--tx);text-decoration:none;letter-spacing:-.02em}}
  .logo span{{background:var(--grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}}
  .nav a{{color:var(--mut);text-decoration:none;margin-left:22px;font-size:14px;font-weight:500}}
  .nav a:hover{{color:var(--tx)}}
  .breadcrumb{{margin:22px auto 0;font-size:12px;color:var(--dim);font-family:'JetBrains Mono',monospace}}
  .breadcrumb a{{color:var(--ind);text-decoration:none}}
  .container{{padding-top:4px;padding-bottom:64px}}
  .hero{{padding:34px 0 6px}}
  .hero h1{{font-size:34px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px}}
  .hero .prov{{color:var(--tx);font-size:16px;font-weight:500;margin-bottom:12px}}
  .hero .loc{{color:var(--mut);font-size:15px}}
  .hero .loc .loc-link{{color:inherit;text-decoration:none;border-bottom:1px dotted var(--dim)}}
  .hero .loc .loc-link:hover{{color:var(--ind);border-bottom-color:var(--ind)}}
  .stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:24px 0}}
  .stat-card{{background:var(--surf);border:1px solid var(--b);border-radius:14px;padding:18px 20px}}
  a.stat-card.stat-link{{text-decoration:none;color:inherit;cursor:pointer;transition:border-color .15s}}
  a.stat-card.stat-link:hover{{border-color:var(--ind)}}
  a.stat-card.stat-link .stat-label::after{{content:' \\2197';opacity:.5;font-size:11px;font-weight:700;color:var(--ind)}}
  a.chip.chip-link{{display:block;text-decoration:none;color:inherit;cursor:pointer;transition:border-color .15s}}
  a.chip.chip-link:hover{{border-color:var(--ind)}}
  .verdict-link{{text-decoration:none}}
  .stat-label{{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;font-family:'JetBrains Mono',monospace}}
  .stat-value{{font-size:19px;font-weight:600;font-family:'JetBrains Mono',monospace;word-break:break-word}}
  .section{{background:var(--surf);border:1px solid var(--b);border-radius:16px;padding:24px;margin:18px 0}}
  .section-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}}
  .section-head h2{{font-size:18px;font-weight:600}}
  .section-sub{{color:var(--mut);font-size:14px;margin-bottom:16px}}
  .verdict{{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;letter-spacing:.06em;padding:5px 12px;border:1px solid;border-radius:999px;white-space:nowrap}}
  .chips{{display:flex;flex-wrap:wrap;gap:10px}}
  .chip{{background:var(--surf2);border:1px solid var(--b);border-radius:10px;padding:10px 14px;min-width:118px}}
  .chip-l{{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-family:'JetBrains Mono',monospace}}
  .chip-v{{display:block;font-size:18px;font-weight:600;font-family:'JetBrains Mono',monospace;margin-top:4px}}
  .link{{display:inline-block;margin-top:16px;color:var(--ind);text-decoration:none;font-weight:600;font-size:14px}}
  .map-block{{padding:0;overflow:hidden}}
  .cta{{background:linear-gradient(135deg,rgba(99,102,241,0.12),rgba(168,85,247,0.06));border:1px solid rgba(99,102,241,0.25);border-radius:16px;padding:22px 24px;margin:18px 0;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;justify-content:center;text-align:center}}
  .cta a{{color:var(--ind);text-decoration:none;font-weight:600;font-size:14px}}
  .cta .primary{{background:var(--grad);color:#fff;padding:10px 18px;border-radius:9px}}
  .foot{{color:var(--dim);font-size:13px;text-align:center;padding-top:24px;border-top:1px solid var(--b);margin-top:30px}}
  .foot a{{color:var(--ind);text-decoration:none}}
</style>
</head>
<body>
  <header class="header">
    <div class="header-inner">
      <a href="/" class="logo">DC<span>Hub</span></a>
      <nav class="nav">
        <a href="/land-power-map">Map</a>
        <a href="/markets">Markets</a>
        <a href="/dcpi">DCPI</a>
        <a href="/api-docs">API</a>
      </nav>
    </div>
  </header>

  <div class="breadcrumb">
    <a href="/">Home</a> · <a href="/facilities">Facilities</a> · {_country_crumb}{_mkt_crumb}{_esc(name)}
  </div>

  <div class="container">
    <div class="hero">
      <h1>{_esc(_disp)}</h1>
      {f'<div class="prov">{_esc(provider)}</div>' if (provider and provider.strip().lower() != name.strip().lower()) else ''}
      <div class="loc">📍 {f'<a href="/dcpi/{_esc(_mslug0)}" class="loc-link">{_esc(loc_short)}</a>' if _mslug0 else _esc(loc_short)}</div>
    </div>

    {narrative_html}

    <div class="stats-grid">{stats_html}</div>

    {dcpi_html}

    {_mkt_context_html}

    {map_block}

    {context_html}
    {comps_html}
    {sponsor_html}

    <div class="cta">
      <a class="primary" href="/pricing">Get all {_CANON['public']['facilities']} facilities + power scores &amp; site-selection tools &mdash; DC Hub from $49/mo &rarr;</a>
      <a href="/sites/{_esc(slug)}" rel="nofollow">Full capacity report</a>
      <a href="/ai">Free MCP key (AI agents)</a>
      <a href="/cited-by">Used by Claude &amp; Cursor</a>
    </div>

    <div class="foot">
      Data: DC Hub global infrastructure database ·
      <a href="/api/v1/facilities/{_esc(slug)}" rel="nofollow">Raw JSON</a>
      <div style="margin-top:8px">Live grid, fiber &amp; DCPI context for this facility &mdash; AI agents: query DC Hub MCP
        <code>get_facility slug="{_esc(slug)}"</code> (endpoint https://dchub.cloud/mcp) for the full cited record. Connect:
        <a href="https://dchub.cloud/connect?src=page-onramp&amp;entity={_esc(slug)}">https://dchub.cloud/connect?src=page-onramp&amp;entity={_esc(slug)}</a></div>
    </div>
  </div>

  <script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""


def _canonical_twin_url(dup_of_id):
    """URL of the surviving facility a duplicate should canonicalise to.

    Returns None unless the target exists, is NOT itself a duplicate, and has a
    real frozen slug — a canonical pointing at a 404 is worse than a
    self-canonical. Fail-soft: any error returns None.
    """
    if not dup_of_id:
        return None
    try:
        # ★ the module's own pattern — a local `from main import get_read_db`.
        # I first wrote `_get_conn()`, which does NOT exist here: it would have
        # raised NameError straight into the `except Exception: return None`
        # below, so this feature would have silently never fired and the tests
        # would still have passed.
        from main import get_read_db
        conn = get_read_db()
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT canonical_slug FROM discovered_facilities "
                    "WHERE id = %s AND COALESCE(is_duplicate, 0) = 0 "
                    "  AND canonical_slug IS NOT NULL AND canonical_slug <> '' "
                    "LIMIT 1",
                    (dup_of_id,))
                row = cur.fetchone()
        finally:
            try: conn.close()
            except Exception: pass
        if row and row[0]:
            return "https://dchub.cloud/facilities/" + str(row[0])
    except Exception:
        return None
    return None


@facility_profile_bp.route("/facilities/<path:slug>", methods=["GET"])
def render_facility_profile(slug):
    """Dynamic facility profile page. Falls back here when the static
    HTML file doesn't exist in CF Pages. Handles both .html-suffixed
    and bare slugs.
    """
    # Strip .html suffix (some old links include it)
    if slug.endswith(".html"):
        slug = slug[:-5]
    # Handle nested paths just in case
    slug = slug.split("/")[0]

    fac = _fetch_facility_by_slug(slug)
    if not fac:
        # r-slug-freeze (2026-07-03): AUTHORITATIVE recovery first — the
        # persistent facility_slug_aliases table (old_slug → frozen canonical)
        # gives a deterministic single-hop 301, killing the multi-hop chains
        # that GSC files as "Redirect error". Only fall through to the fuzzy
        # name-token resolver when we have no stored alias.
        try:
            from routes.facility_slug_freeze import resolve_alias
            _alias = resolve_alias(slug)
        except Exception:
            _alias = None
        if _alias and _alias != slug:
            return Response(status=301, headers={
                "Location": f"/facilities/{_alias}",
                "Cache-Control": "public, max-age=86400",
                "X-DC-Hub-Source": "facility-slug-alias",
            })
        # SEO recovery: re-ingestion churns MD5(id) → old indexed slugs 404
        # though the facility still exists under a new slug. Resolve + 301 to
        # the current canonical URL (preserves link equity). Only confident
        # matches redirect; the rest keep the existing noindex 404.
        _target = _resolve_legacy_slug(slug)
        if _target and _target != slug:
            return Response(status=301, headers={
                "Location": f"/facilities/{_target}",
                "Cache-Control": "public, max-age=86400",
                "X-DC-Hub-Source": "facility-slug-recovery",
            })
        return Response(
            f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><title>Facility not found | DC Hub</title>
<meta name="robots" content="noindex"></head>
<body style="font-family:system-ui;background:#09090b;color:#fafafa;
text-align:center;padding:80px 20px">
<h1>Facility not found</h1>
<p style="color:#888">No facility matches slug <code>{_esc(slug)}</code>.</p>
<p><a href="/land-power-map" style="color:#6366f1">Browse the map</a> ·
<a href="/" style="color:#6366f1">Home</a></p>
</body></html>""",
            status=404, mimetype="text/html"
        )

    html = _render_profile(fac, slug)
    # r-page-onramp (2026-07-04): citation header with as-of stamp. ASCII only
    # (headers are latin-1; the industry-pulse em-dash 502 is the trap).
    _cite = _ascii_header(
        f"DC Hub Facility {slug} - as of {_dt.date.today().isoformat()}")
    return Response(html, status=200, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600",
                             "X-DC-Hub-Source": "facility-profile-dynamic",
                             "X-Cite-As": _cite})
