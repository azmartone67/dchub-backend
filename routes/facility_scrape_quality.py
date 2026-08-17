"""facility_scrape_quality.py — the provider-website scrape landed PAGES, not
facilities (2026-08-08).

`discovery_nexus.ProviderWebsitesSource` walks eight operator /data-centers
pages and turns every matching <a> into a Facility. Its only filters are a
length bound and six stop-words, so a metro index link and a "Smart Hands"
product link become facility rows exactly like a building link does. One run,
2026-03-18, put 312 rows into `discovered_facilities` and nothing has run it
since (no module imports `discovery_nexus`, and `NexusEngine` writes SQLite).

MEASURED on the live table, 2026-08-08 — the reported "312 junk rows" is NOT
what is there. Three sub-shapes, only one of which is unambiguous:

  34  page furniture   name is no place at all: 'Equinix Smart Hands®',
                       'Cages and cabinets', 'See our EMEA facilities',
                       'APAC', 'French', 'xScaleEnable multi-megawatt,
                       AI-ready capacity'. Never a facility, under any reading.
 181  metro landing    name is a bare metro: 'Amsterdam', 'Chicago Data
                       Centers'. Not a building — but 50 of them are the only
                       row we hold for that provider+city, and the shape also
                       catches real single-site campuses Vantage names by
                       location alone ('Fredericksburg, VA, United States').
                       REPORTED, NOT ACTED ON: see the analyze payload.
  97  building grain   'Sterling, VA, NVA1-NVA3', 'Atlanta - Alpharetta',
                       'Ashburn II'. Real facilities.

THE SEPARATE, LARGER DEFECT — the page locale was written as the location.
ALL 312 rows carry `market='London'`, and 162 also carry `city='London'`,
including 'CyrusOne Frankfurt, FRA1' and 'Vantage Berlin I, Germany'. Only 9
of them are in London. /dcpi/london is an intl market, so its scope is
`country NOT IN ('US','USA')` — which credits `country=''` — and it counted
140 of these rows out of 432 total. 131 of that 140 belong to another market
entirely: a THIRD of London's published facility count.

★ NEITHER DUPLICATE FLAG FIXES THIS. The DCPI facility count is
`COUNT(*) ... WHERE LOWER(city)=LOWER(%s) {country_scope}` in
`gather_metrics_for_market` (routes/dcpi.py) and it is deliberately not
duplicate-scoped — r-list-dedup (#2386, 2026-08-08) says so in a comment
right above it, because adding the predicate rescores 272 of 301 markets and
needs its own PR. So `is_duplicate` and `duplicate_of_id` both move the
sitemap and the verified count while leaving a market's tally exactly where
it was. The lever that works is `city` / `market`, which is why this repair
writes those and not just a flag. (#2386 did add `duplicate_of_id IS NULL` to
the market LIST query in `public_market_page` — a pointer would work there,
but these rows have none, and NULLing city/market drops them from that query
regardless since it matches on `market = %s OR LOWER(city) = LOWER(%s)`.)

★ AND A FLAG ALONE WOULD BE UNDONE. All 34 page-furniture rows are ALONE in
their `canonical_slug` group, so flagging them `is_duplicate=1` creates 34
keeperless groups — precisely what `repair_dedup_keeper_election.py` exists to
re-elect a keeper for. That script's ELECTION_SQL is guarded here by a
`dedup_method` exclusion added in the same change.

FIX — two rules, both reversible, both scoped to source='providerwebsites':

  pw_page_furniture   city=NULL, market=NULL, is_duplicate=1,
                      dedup_method='pw_page_furniture'.
                      Belongs to no market and is not a facility.

  pw_page_locale      city := the place the row's own NAME leads with,
                      market=NULL.
                      The scraper lost the location into a page-locale string,
                      but the link text it captured still carries it verbatim.
                      market is CLEARED rather than re-stamped: we have no
                      metro taxonomy here, and an empty market is honest where
                      'London' is a lie. The row still reaches its real market
                      through `city`, which is the column both DCPI queries
                      actually match on.

NOT TOUCHED: `country`. For the bare-metro rows the country is the one field
that is right — see reference_dchub_discovered_country_repair_0808. The 9 rows
whose own name says London keep city='London' and market='London'.

Endpoints (admin-keyed, DCHUB_ADMIN_KEY):
  GET  /api/v1/admin/facility-scrape/analyze          dry-run report
  POST /api/v1/admin/facility-scrape/apply?confirm=1  write
  POST /api/v1/admin/facility-scrape/undo?confirm=1   revert, flag-scoped
Kill switch: FACILITY_SCRAPE_QUALITY_DISABLE=1
"""
from __future__ import annotations

import os
import logging
from collections import Counter

from flask import Blueprint, request, jsonify

from util.scraped_page_title import (
    is_page_furniture, has_building_grain, leading_place,
)

logger = logging.getLogger("facility_scrape_quality")
facility_scrape_quality_bp = Blueprint("facility_scrape_quality", __name__)

SOURCE = "providerwebsites"
FLAG_FURNITURE = "pw_page_furniture"
FLAG_LOCALE = "pw_page_locale"
# The page-locale string this scrape stamped on every row it produced.
PAGE_LOCALE = "London"


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


def _disabled():
    return (os.environ.get("FACILITY_SCRAPE_QUALITY_DISABLE") or "").strip() == "1"


def plan_row(rid, name, city, market, provider=""):
    """The write this row needs, or None to leave it alone.

    Pure and importable so tests can pin the classification without a DB.
    `provider` is what stops a CamelCase brand reading as a collapsed heading.
    """
    city = (city or "").strip()
    market = (market or "").strip()
    locale = PAGE_LOCALE.lower()

    if is_page_furniture(name, provider):
        return {"id": rid, "rule": FLAG_FURNITURE, "name": name,
                "city_to": None, "suppress": True}

    place = leading_place(name)
    name_says_elsewhere = bool(place) and place.lower() != locale
    # A row really is in the locale city only when nothing contradicts it —
    # 'CyrusOne London, LON1' does not, 'CyrusOne Frankfurt, FRA1' does.
    really_in_locale = (city.lower() == locale or not city) \
        and not name_says_elsewhere

    city_wrong = (city.lower() == locale or not city) and name_says_elsewhere
    market_wrong = market.lower() == locale and not really_in_locale
    if not (city_wrong or market_wrong):
        return None
    return {"id": rid, "rule": FLAG_LOCALE, "name": name,
            "city_to": place if city_wrong else (city or None),
            "suppress": False}


def _scan():
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            # scrape_flag does not exist until the first /apply, so analyze has
            # to read the table as it is, not as it will be.
            _flag = "scrape_flag" if _has_flag_col(cur) else "NULL"
            cur.execute(f"""
                SELECT id, name, city, market, provider, country, {_flag}
                  FROM discovered_facilities
                 WHERE source = %s
                 ORDER BY id
            """, (SOURCE,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass

    furniture, locale, landing, keep, done = [], [], [], [], []
    for rid, name, city, market, provider, country, flag in rows:
        p = plan_row(rid, name, city, market, provider)
        rec = {"id": rid, "name": name, "provider": provider,
               "city": city, "market": market, "country": country,
               "already": flag}
        if flag:
            # ★ALREADY WRITTEN, SO NOT WORK. apply's UPDATE carries
            # `AND scrape_flag IS NULL`, so a flagged row can only produce a
            # rowcount of 0 — but the prefilter is `source = %s`, which nothing
            # this lane writes can escape, so without this branch the furniture
            # bucket re-reports its own output forever. The locale bucket left
            # on its own because apply rewrites `city` and nulls `market`, which
            # is enough to make plan_row return None; the furniture rule keys on
            # `name` and `provider`, which apply never touches, so all 34 live
            # furniture rows re-planned as furniture on every run (measured
            # 2026-08-16). `flag` was already being READ into rec["already"] and
            # then ignored.
            done.append(rec)
        elif p and p["rule"] == FLAG_FURNITURE:
            furniture.append(rec)
        elif p:
            rec["city_to"] = p["city_to"]
            locale.append(rec)
        else:
            keep.append(rec)
        # orthogonal read: metro-landing shape, reported but never written.
        # Deliberately still computed over EVERY row — it is a shape inventory,
        # not a work queue, so an applied row is still part of the shape.
        if not is_page_furniture(name, provider) and not has_building_grain(name):
            landing.append(rec)
    return {"total": len(rows), "furniture": furniture, "locale": locale,
            "landing": landing, "untouched": keep, "already_applied": done}


def _has_flag_col(cur):
    try:
        cur.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='discovered_facilities' "
                    "AND column_name='scrape_flag'")
        return cur.fetchone() is not None
    except Exception:
        return False


@facility_scrape_quality_bp.route("/api/v1/admin/facility-scrape/analyze")
def scrape_analyze():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    s = _scan()
    if s is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    moves = Counter((r["city"] or "", r["city_to"] or "") for r in s["locale"])
    sample = request.args.get("sample") == "1"
    return jsonify(
        ok=True, dry_run=True, source=SOURCE, total=s["total"],
        page_furniture=len(s["furniture"]),
        page_locale=len(s["locale"]),
        untouched=len(s["untouched"]),
        # rows this lane has already written — reported separately so the two
        # counts above mean "outstanding work" rather than "ever matched"
        already_applied=len(s["already_applied"]),
        # reported for a follow-up decision, NEVER written by /apply
        metro_landing_shape=len(s["landing"]),
        top_moves=[{"from": f, "to": t, "n": n}
                   for (f, t), n in moves.most_common(20)],
        furniture_sample=[r["name"] for r in s["furniture"]][:40] if sample else None,
        landing_sample=[r["name"] for r in s["landing"]][:40] if sample else None,
    ), 200


@facility_scrape_quality_bp.route("/api/v1/admin/facility-scrape/apply",
                                  methods=["POST"])
def scrape_apply():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    s = _scan()
    if s is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True,
                       would_suppress=len(s["furniture"]),
                       would_relocate=len(s["locale"]),
                       note="add ?confirm=1 to write"), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    suppressed = relocated = 0
    try:
        with c.cursor() as cur:
            # Originals are preserved in their own columns rather than
            # inferred from the flag: /undo must restore what was there, not
            # what this module assumed was there.
            cur.execute("ALTER TABLE discovered_facilities "
                        "ADD COLUMN IF NOT EXISTS scrape_flag TEXT")
            cur.execute("ALTER TABLE discovered_facilities "
                        "ADD COLUMN IF NOT EXISTS scrape_city_orig TEXT")
            cur.execute("ALTER TABLE discovered_facilities "
                        "ADD COLUMN IF NOT EXISTS scrape_market_orig TEXT")
            for r in s["furniture"]:
                try:
                    cur.execute(
                        "UPDATE discovered_facilities SET "
                        "  scrape_city_orig = COALESCE(scrape_city_orig, city), "
                        "  scrape_market_orig = COALESCE(scrape_market_orig, market), "
                        "  city = NULL, market = NULL, is_duplicate = 1, "
                        "  dedup_method = %s, scrape_flag = %s "
                        "WHERE id = %s AND source = %s AND scrape_flag IS NULL",
                        (FLAG_FURNITURE, FLAG_FURNITURE, r["id"], SOURCE))
                    suppressed += cur.rowcount
                except Exception as e:
                    logger.warning("[scrape] furniture id=%s: %s", r["id"], str(e)[:120])
            for r in s["locale"]:
                try:
                    cur.execute(
                        "UPDATE discovered_facilities SET "
                        "  scrape_city_orig = COALESCE(scrape_city_orig, city), "
                        "  scrape_market_orig = COALESCE(scrape_market_orig, market), "
                        "  city = %s, market = NULL, scrape_flag = %s "
                        "WHERE id = %s AND source = %s AND scrape_flag IS NULL",
                        (r["city_to"], FLAG_LOCALE, r["id"], SOURCE))
                    relocated += cur.rowcount
                except Exception as e:
                    logger.warning("[scrape] locale id=%s: %s", r["id"], str(e)[:120])
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, table="discovered_facilities",
                   suppressed=suppressed, relocated=relocated,
                   metro_landing_left=len(s["landing"])), 200


@facility_scrape_quality_bp.route("/api/v1/admin/facility-scrape/undo",
                                  methods=["POST"])
def scrape_undo():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True,
                       note="add ?confirm=1 to revert"), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    n = 0
    try:
        with c.cursor() as cur:
            # Scoped on scrape_flag, so an undo cannot revert the 2026-08-07
            # country repair's rows or anything else that shares this table.
            cur.execute(
                "UPDATE discovered_facilities SET "
                "  city = scrape_city_orig, market = scrape_market_orig, "
                "  is_duplicate = CASE WHEN scrape_flag = %s THEN 0 "
                "                      ELSE is_duplicate END, "
                "  dedup_method = CASE WHEN dedup_method = %s THEN NULL "
                "                      ELSE dedup_method END, "
                "  scrape_flag = NULL, scrape_city_orig = NULL, "
                "  scrape_market_orig = NULL "
                "WHERE scrape_flag IN (%s, %s)",
                (FLAG_FURNITURE, FLAG_FURNITURE, FLAG_FURNITURE, FLAG_LOCALE))
            n = cur.rowcount
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, reverted=n), 200
