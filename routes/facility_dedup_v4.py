"""facility_dedup_v4.py — duplicate PUBLISHED URLs, keyed on what the page renders.

WHY A FOURTH LANE (2026-09-07)
------------------------------
Every earlier lane groups on DB columns of ONE table:

    facility_dedup.py      (name, city) on `facilities`            — the legacy
                           table. Never affected a URL Google sees before the
                           2026-07-01 sitemap union, and still cannot reach the
                           rows that make most of them.
    facility_dedup_v3.py   (name, city) on `discovered_facilities`, and only
                           the anonymous-provider signature (one real operator
                           + a blank/'Unknown' twin). 601 groups.

Neither can see the population that is actually published twice, because the
sitemap's facility set is a UNION of BOTH tables and the duplicate pair
straddles them.

★★★ MEASURED 2026-09-07, live /sitemap.xml (8 shards, 23,094 facility URLs),
each URL resolved back to the row that serves it and rendered:

    3,989 groups of >=2 URLs share a byte-identical <h1> AND <title>
    4,007 surplus URLs
    group composition:  df+lg 3,893 · df+df 82 · everything else 14
    every member: HTTP 200, "index, follow", rel=canonical at ITSELF
    7,665 of 7,996 members carry duplicate_of_id IS NULL
    7,734 of 7,996 carry is_duplicate = 0

So the pairs were never DETECTED — not detected and mishandled. 97.6% of them
straddle the two tables, which is why a same-table detector reported nothing.

THE KEY: WHAT THE PAGE RENDERS, NOT WHAT THE ROW STORES
-------------------------------------------------------
<h1> and <title> are a pure function of (name, provider, city, state, country)
— util/facility_headline. Two URLs whose pages render the same h1 AND the same
title ARE one facility as far as a crawler is concerned, whatever the rows say.

The slug cannot be that key: it hashes provider|name, and the classic pair
disagrees about `provider` precisely because one row was drained from the
other. The id cannot be it either: discovered_facilities.id is INTEGER and
facilities.id is TEXT — two id spaces.

★ This module imports the renderer's own composition (util.facility_headline,
  which routes/facility_profile_page._render_profile now calls) rather than
  restating it. A detector scoring a copy of the page instead of the page is
  the failure mode this exists to avoid.

WHAT IT WRITES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
★★ POINTER-ONLY. `is_duplicate` is a VISIBILITY flag: setting it drops the row
   from every filtered COUNT and from the sitemap, and buys nothing
   rel=canonical does not. Setting it once (2026-07-28) left 57 of 58 slugs
   with NO keeper and was reverted. A `duplicate_of_id` alone makes the page
   emit rel=canonical at its twin while the row stays live, counted, and
   serving 200. Suppression deletes a page; a canonical MERGES it.

★★ IT WRITES ONLY `discovered_facilities.duplicate_of_id`, and only when BOTH
   the keeper and the alternate are discovered rows. That is the whole id space
   the column addresses. The other two classes are REPORTED, never guessed at:

     drain_fork_no_write   the alternate is a legacy row the drain forked off
                           this very keeper (discovered.merged_facility_id =
                           facilities.id). It already canonicalises correctly
                           via routes/facility_profile_page._drained_twin_url
                           and is already dropped from the sitemap by
                           main._drained_keeper — 3,667 of the 3,989 groups.
                           Nothing to write; writing anything would be inventing
                           a second, disagreeing pointer.

     unlinkable_legacy     the alternate is a legacy row with NO link to the
                           keeper (195 PeeringDB, 13 OSM, … — independently
                           ingested rows that happen to render identically).
                           `facilities.duplicate_of_id` is TEXT and addresses
                           facilities.id, so it CANNOT point at the discovered
                           keeper. Consolidating these needs a cross-table
                           pointer column that does not exist yet. ~251 groups.
                           Reported so the residual is visible, never silently
                           counted as fixed.

SAFETY RULES (each one is a measured lesson, not a precaution)
-------------------------------------------------------------
★ MAX_GROUP is 4. The live histogram is {2: 3,976 · 3: 11 · 5: 1 · 6: 1}; a
  group bigger than that is a generic-name collision, not a facility. Refused
  and reported, never merged. (v3's lesson: 581 of 1,205 (name, city) groups
  were Amazon IAD85/IAD75/IAD96 — distinct buildings under a generic name.)
★ Coordinates VETO a merge, never justify one. Rows without coordinates do not
  block (a missing coordinate is not evidence of distance); two KNOWN
  coordinates more than _COORD_EPS apart stop the group.
★ A row must have a real name. `_render_profile` defaults a NULL name to "Data
  Center", so nameless rows would all group into one enormous false cluster.
★ Junk slugs ('unknown-%', numeric-OSM) are excluded at source — they are
  noindex and not sitemapped, so merging them is work with no reader.
★ Never overwrite an existing pointer: `duplicate_of_id IS NULL` in the WHERE,
  re-asserted at WRITE time, so another lane's verdict always wins over ours.

Endpoints (admin-keyed):
  GET  /api/v1/admin/facility-dedup-v4/analyze[?limit=]   dry run, no writes
  POST /api/v1/admin/facility-dedup-v4/apply?confirm=1    write pointers
  POST /api/v1/admin/facility-dedup-v4/undo?confirm=1     clear THIS lane only

Kill switch, no deploy: FACILITY_DEDUP_V4_DISABLE=1
Scheduled by routes/cron_heartbeat.py (facility_dedup_v4_daily).
"""
from __future__ import annotations

import os
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger("facility_dedup_v4")
facility_dedup_v4_bp = Blueprint("facility_dedup_v4", __name__)

DEDUP_METHOD = "rendered-identity/v4"

# ~2km at the equator. A VETO threshold, not a matching signal.
_COORD_EPS = 0.02

# Above this many distinct URLs a shared identity is a generic-name collision,
# not one facility. Measured live: the whole population is 2s and 3s.
MAX_GROUP = 4


def _disabled():
    return (os.environ.get("FACILITY_DEDUP_V4_DISABLE") or "").strip() == "1"


def _admin_ok():
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    sent = ((request.headers.get("X-Admin-Key")
             or request.headers.get("X-Internal-Key")
             or request.args.get("admin_key") or "").strip())
    return bool(expected) and sent == expected


def _conn(write=False):
    """Connection. `write=True` MUST return a primary, never the replica.

    ★ v3's first apply died with "cannot execute UPDATE in a read-only
    transaction" because it hand-rolled psycopg2.connect(DATABASE_URL) and
    landed on a read-only endpoint in the web process. The app's pooled
    main.get_db() is the blessed writable path; the raw DSN is a fallback only.
    """
    if write:
        try:
            from main import get_db
            c = get_db()
            if c is not None:
                try: c.autocommit = True
                except Exception: pass
                return c
        except Exception as e:
            logger.warning("facility_dedup_v4: get_db unavailable: %s", e)
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("facility_dedup_v4: connect failed: %s", e)
        return None


def _conn_diag(c):
    """What did we actually connect to? Reported by /analyze so a read-only
    surprise is visible BEFORE apply fails, not after."""
    try:
        with c.cursor() as cur:
            cur.execute("SELECT current_setting('transaction_read_only'), "
                        "       pg_is_in_recovery()")
            ro, rec = cur.fetchone()
        return {"read_only": ro, "in_recovery": bool(rec)}
    except Exception as e:
        return {"error": str(e)[:120]}


def is_junk_slug(slug: str) -> bool:
    """Slugs the sitemap already refuses to emit and the page already
    noindexes — mirrors main._build_sitemap_sections' r-junk-prune guard."""
    import re as _re
    s = (slug or "")
    if not s or s.startswith("unknown-"):
        return True
    return bool(_re.search(r"(?:^|-)data-center-\d{6,}(?:-|$)", s))


def plan_group(rows):
    """Decide ONE rendered-identity group. PURE — no I/O, unit-tested.

    `rows` = [{table, id, canonical_slug, provider, latitude, longitude,
               power_mw, merged_facility_id}, ...]  (>=1 row per URL)

    Returns {"keeper": row|None, "writes": [ids], "drain_fork": [slugs],
             "unlinkable": [slugs], "skip": reason|None}.
    """
    slugs = {r["canonical_slug"] for r in rows if r.get("canonical_slug")}
    if len(slugs) < 2:
        return {"keeper": None, "writes": [], "drain_fork": [],
                "unlinkable": [], "skip": "single_url"}
    if len(slugs) > MAX_GROUP:
        return {"keeper": None, "writes": [], "drain_fork": [],
                "unlinkable": [], "skip": "group_too_large"}

    # ★ Coordinates VETO. Known coordinates only; a missing one does not block.
    lats = [r["latitude"] for r in rows if r.get("latitude") is not None]
    lons = [r["longitude"] for r in rows if r.get("longitude") is not None]
    if lats and (max(lats) - min(lats)) > _COORD_EPS:
        return {"keeper": None, "writes": [], "drain_fork": [],
                "unlinkable": [], "skip": "coords_far_apart"}
    if lons and (max(lons) - min(lons)) > _COORD_EPS:
        return {"keeper": None, "writes": [], "drain_fork": [],
                "unlinkable": [], "skip": "coords_far_apart"}

    # ★ THE KEEPER IS A DISCOVERED ROW, ALWAYS. It is the row the discovery and
    # enrichment pipelines keep updating, the row /facilities/<slug> resolution
    # already prefers, the row the sitemap emits first, and the only id space
    # `duplicate_of_id` can address. Richest first, then lowest id, so the same
    # group always plans the same way.
    cand = [r for r in rows
            if r["table"] == "discovered_facilities"
            and (r.get("canonical_slug") or "").strip()
            and r.get("duplicate_of_id") is None]
    if not cand:
        return {"keeper": None, "writes": [], "drain_fork": [],
                "unlinkable": [], "skip": "no_discovered_keeper"}
    cand.sort(key=lambda r: (-(r.get("power_mw") or 0), r["id"]))
    keeper = cand[0]

    writes, drain_fork, unlinkable = [], [], []
    for r in rows:
        if r["canonical_slug"] == keeper["canonical_slug"]:
            continue
        if r["table"] == "discovered_facilities":
            # ★ never overwrite another lane's verdict
            if r.get("duplicate_of_id") is None and r["id"] != keeper["id"]:
                writes.append(r["id"])
        elif str(keeper.get("merged_facility_id") or "") == str(r["id"]):
            # the drain forked this legacy row off THIS keeper — the read path
            # already consolidates it; writing anything here would create a
            # second pointer that could disagree.
            drain_fork.append(r["canonical_slug"])
        else:
            unlinkable.append(r["canonical_slug"])

    if not writes and not drain_fork and not unlinkable:
        return {"keeper": None, "writes": [], "drain_fork": [],
                "unlinkable": [], "skip": "nothing_to_do"}
    return {"keeper": keeper, "writes": writes, "drain_fork": drain_fork,
            "unlinkable": unlinkable, "skip": None}


# ★ The publishable universe, both tables. A SUPERSET of what the sitemap emits
#   (the capacity gate narrows it further) — deliberately, because consolidating
#   a pair BEFORE it is published is strictly better than after. `name` must be
#   real: _render_profile defaults a NULL name to "Data Center", so nameless
#   rows would collapse into one enormous false group.
_SQL_DISCOVERED = """
    SELECT 'discovered_facilities' AS tbl, id::text, canonical_slug,
           name, provider, city, state, country,
           latitude, longitude, power_mw, duplicate_of_id, merged_facility_id
      FROM discovered_facilities
     WHERE COALESCE(is_duplicate, 0) = 0
       AND canonical_slug IS NOT NULL AND canonical_slug <> ''
       AND name IS NOT NULL AND trim(name) <> ''
"""

_SQL_LEGACY = """
    SELECT 'facilities' AS tbl, id::text, canonical_slug,
           name, provider, city, state, country,
           latitude, longitude, power_mw, NULL::int, NULL::text
      FROM facilities
     WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''
       AND name IS NOT NULL AND trim(name) <> ''
"""


def _collect(cur, limit=None):
    """Group the publishable universe by rendered identity. (plans, stats)."""
    from util.facility_headline import identity_key
    import collections

    groups = collections.defaultdict(list)
    for sql in (_SQL_DISCOVERED, _SQL_LEGACY):
        cur.execute(sql)
        for r in cur.fetchall() or []:
            slug = r[2]
            if is_junk_slug(slug):
                continue
            groups[identity_key(r[3], r[4], r[5], r[6], r[7])].append({
                "table": r[0], "id": int(r[1]) if r[0] == "discovered_facilities" else r[1],
                "canonical_slug": slug, "provider": r[4],
                "latitude": r[8], "longitude": r[9], "power_mw": r[10],
                "duplicate_of_id": r[11], "merged_facility_id": r[12],
            })

    plans, stats = [], {}
    for key, rows in groups.items():
        if len({r["canonical_slug"] for r in rows}) < 2:
            continue
        p = plan_group(rows)
        if p["skip"]:
            stats[p["skip"]] = stats.get(p["skip"], 0) + 1
            continue
        if p["writes"]:
            stats["writable"] = stats.get("writable", 0) + 1
        if p["drain_fork"]:
            stats["drain_fork_no_write"] = stats.get("drain_fork_no_write", 0) + 1
        if p["unlinkable"]:
            stats["unlinkable_legacy"] = stats.get("unlinkable_legacy", 0) + 1
        plans.append({"h1": key[0], "keeper_id": p["keeper"]["id"],
                      "keeper_slug": p["keeper"]["canonical_slug"],
                      "writes": p["writes"], "drain_fork": p["drain_fork"],
                      "unlinkable": p["unlinkable"]})
        if limit and len(plans) >= limit:
            break
    return plans, stats


def _write_diag():
    """Probe the WRITE connection during a dry run — a read-only endpoint must
    surface in /analyze, not as a 500 halfway through /apply."""
    c = _conn(write=True)
    if c is None:
        return {"error": "no_write_conn"}
    try:
        return _conn_diag(c)
    finally:
        try: c.close()
        except Exception: pass


def _summary(plans, stats):
    return {
        "method": DEDUP_METHOD,
        "mode": "pointer_only (never sets is_duplicate)",
        "groups": len(plans),
        "pointers_writable": sum(len(p["writes"]) for p in plans),
        "drain_fork_urls_no_write": sum(len(p["drain_fork"]) for p in plans),
        "unlinkable_legacy_urls": sum(len(p["unlinkable"]) for p in plans),
        "skipped": stats,
    }


@facility_dedup_v4_bp.route("/api/v1/admin/facility-dedup-v4/analyze")
def analyze():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin_key_required"), 401
    conn = _conn()
    if conn is None:
        return jsonify(ok=False, error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            plans, stats = _collect(
                cur, int(request.args.get("limit") or 0) or None)
        out = _summary(plans, stats)
        out.update(ok=True, dry_run=True, write_conn=_write_diag(),
                   sample=plans[:20])
        return jsonify(out), 200
    except Exception as e:
        logger.warning("facility_dedup_v4 analyze failed: %s", e)
        return jsonify(ok=False, error=str(e)[:300]), 500
    finally:
        try: conn.close()
        except Exception: pass


@facility_dedup_v4_bp.route("/api/v1/admin/facility-dedup-v4/apply",
                            methods=["POST"])
def apply():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin_key_required"), 401
    if request.args.get("confirm") != "1":
        return jsonify(ok=False, error="confirm=1 required",
                       hint="run /analyze first"), 400
    conn = _conn(write=True)
    if conn is None:
        return jsonify(ok=False, error="db_unavailable"), 503
    marked = 0
    try:
        with conn.cursor() as cur:
            plans, stats = _collect(
                cur, int(request.args.get("limit") or 0) or None)
            for p in plans:
                if not p["writes"]:
                    continue
                # ★ The WHERE re-asserts every precondition at WRITE time: a row
                # another lane pointed between analyze and apply is left alone,
                # a suppressed row is left alone, and a row can never be made
                # its own duplicate.
                # ★★ is_duplicate is DELIBERATELY NOT WRITTEN — see the module
                # docstring. Pointer only.
                cur.execute(
                    "UPDATE discovered_facilities "
                    "   SET duplicate_of_id = %s, dedup_method = %s "
                    " WHERE id = ANY(%s) "
                    "   AND duplicate_of_id IS NULL "
                    "   AND COALESCE(is_duplicate, 0) = 0 "
                    "   AND id <> %s",
                    (p["keeper_id"], DEDUP_METHOD, p["writes"], p["keeper_id"]))
                marked += cur.rowcount or 0
        out = _summary(plans, stats)
        out.update(ok=True, applied=True, rows_marked=marked,
                   note="reversible: POST .../facility-dedup-v4/undo?confirm=1")
        return jsonify(out), 200
    except Exception as e:
        logger.warning("facility_dedup_v4 apply failed: %s", e)
        return jsonify(ok=False, error=str(e)[:300], rows_marked=marked), 500
    finally:
        try: conn.close()
        except Exception: pass


@facility_dedup_v4_bp.route("/api/v1/admin/facility-dedup-v4/undo",
                            methods=["POST"])
def undo():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin_key_required"), 401
    if request.args.get("confirm") != "1":
        return jsonify(ok=False, error="confirm=1 required"), 400
    conn = _conn(write=True)
    if conn is None:
        return jsonify(ok=False, error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            # ★ dedup_method = THIS lane's stamp only. A v4 undo must never roll
            # back a v2/v3 decision. is_duplicate is not touched on the way out
            # either — this lane never sets it, so "restoring" it would be
            # writing a value we did not change.
            cur.execute(
                "UPDATE discovered_facilities "
                "   SET duplicate_of_id = NULL, dedup_method = NULL "
                " WHERE dedup_method = %s "
                "   AND COALESCE(is_duplicate, 0) = 0", (DEDUP_METHOD,))
            n = cur.rowcount or 0
        return jsonify(ok=True, rows_cleared=n, method=DEDUP_METHOD), 200
    except Exception as e:
        logger.warning("facility_dedup_v4 undo failed: %s", e)
        return jsonify(ok=False, error=str(e)[:300]), 500
    finally:
        try: conn.close()
        except Exception: pass
