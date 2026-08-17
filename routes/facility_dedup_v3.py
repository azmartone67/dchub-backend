"""facility_dedup_v3.py — anonymous-provider variants (2026-07-28).

WHY THIS EXISTS AND WHY IT IS A SEPARATE MODULE
-----------------------------------------------
routes/facility_dedup.py reads and writes ONLY the legacy `facilities` table
(`FROM facilities`, `UPDATE facilities SET duplicate_of_id`). Every indexable
/facilities/<slug> page is rendered from `discovered_facilities`. So the admin
dedup endpoint has never been able to affect a single URL Google sees — it is
not missing these clusters, it never looks at that table.

Measured 2026-07-28 on `discovered_facilities`: 1,205 groups share a
(name, city) but hold DIFFERENT canonical_slugs, i.e. multiple live URLs for
what looks like one site.

★★ MOST OF THOSE MUST NOT BE MERGED. Inspection of the real rows:

    'amazon web services' @ manassas  -> 4 URLs
        Amazon IAD85   38.779,-77.542
        Amazon IAD75   38.779,-77.543
        Amazon IAD96   38.780,-77.540

Three genuinely distinct AWS buildings that happen to share a generic name.
581 of the 1,205 groups are that shape. facility_dedup.py's docstring already
says it: "a missed duplicate is safe; a false merge hides a real distinct site
and is not." The v2 pass was RIGHT to leave them alone.

THE ONE SIGNATURE THIS MODULE ACTS ON
-------------------------------------
Same name, same city, exactly ONE distinct real provider, at least one row whose
provider is blank/NULL/'Unknown', and every coordinate inside ~2km:

    'brainserve' @ crissier
        BrainServe SA   46.545,6.574  -> brainserve-sa-brainserve-a3c43931
        (blank)         46.547,6.573  -> brainserve-1d8a2e5a
        Unknown         46.547,6.573  -> unknown-brainserve-dd3fd3d9

The anonymous rows are the SAME building re-ingested from a source that did not
carry an operator. They earn a separate URL only because the slug embeds the
provider. 601 groups / 604 redundant URLs.

★ Two real providers in a group => SKIP, always. That single rule is what keeps
  the IAD85/IAD75/IAD96 class safe, and it is asserted by a test.
★ Coordinates are a VETO, never a merge signal (facility_dedup.py's lesson:
  country-centroid placeholders collide, real twins drift). A group whose rows
  sit >2km apart is skipped even when the provider pattern matches.
★ Non-destructive and reversible: sets is_duplicate/duplicate_of_id/dedup_method
  only. /undo clears ONLY rows stamped by THIS module, so a v2 decision can
  never be rolled back by a v3 undo.
★ Never re-flags a row another pass already flagged, and never flags a row that
  has no canonical target with a real slug.
★ ...and never COUNTS one either (fixed 2026-08-16). The scan's gate reads
  `is_duplicate`, which this lane deliberately never writes, so it could not see
  its own output: /analyze reported `urls_removed: 675` where apply would mark
  58. `_collect` now subtracts rows that already carry a pointer — arithmetic
  only, grouping and canonical election untouched. See _collect.

Endpoints (admin-keyed):
  GET  /api/v1/admin/facility-dedup-v3/analyze[?limit=&country=]   dry run
  POST /api/v1/admin/facility-dedup-v3/apply?confirm=1             mark
  POST /api/v1/admin/facility-dedup-v3/undo?confirm=1              unmark

Kill switch, no deploy: FACILITY_DEDUP_V3_DISABLE=1
"""
from __future__ import annotations

import os
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger("facility_dedup_v3")
facility_dedup_v3_bp = Blueprint("facility_dedup_v3", __name__)

DEDUP_METHOD = "anon-provider-variant/v3"

# ~2km at the equator. A VETO threshold, not a matching signal.
_COORD_EPS = 0.02

# Provider strings that carry no operator identity.
_ANON = ("", "unknown", "n/a", "na", "none", "null", "-")


def _conn(write=False):
    """Connection. `write=True` MUST return a primary, never the replica.

    ★★ The first apply died with "cannot execute UPDATE in a read-only
    transaction" while analyze read happily. A hand-rolled
    psycopg2.connect(DATABASE_URL) landed on a read-only endpoint in the web
    process — the exact trap the house `_write_conn()` helpers exist for. The
    app's own pooled get_db() is the blessed writable path (the sitemap
    rebuild writes through it from this same process), so use that first and
    keep the raw DSN only as a fallback.
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
            logger.warning("facility_dedup_v3: get_db unavailable: %s", e)
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("facility_dedup_v3: connect failed: %s", e)
        return None


def _conn_diag(c):
    """What did we actually connect to? Reported by /analyze so a read-only
    surprise is visible BEFORE apply fails, not after."""
    try:
        with c.cursor() as cur:
            cur.execute("SELECT current_setting('transaction_read_only'), "
                        "       pg_is_in_recovery(), inet_server_addr()::text")
            ro, rec, host = cur.fetchone()
        return {"read_only": ro, "in_recovery": bool(rec), "server": str(host)}
    except Exception as e:
        return {"error": str(e)[:120]}


def _admin_ok():
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    sent = ((request.headers.get("X-Admin-Key")
             or request.args.get("admin_key") or "").strip())
    return bool(expected) and sent == expected


def _disabled():
    return (os.environ.get("FACILITY_DEDUP_V3_DISABLE") or "").strip() == "1"


def is_anonymous(provider) -> bool:
    """True when a provider string carries no operator identity."""
    return (provider or "").strip().lower() in _ANON


def plan_group(rows):
    """Decide a single (name, city) group. PURE — no I/O, unit-tested.

    `rows` = [{id, provider, canonical_slug, latitude, longitude, power_mw}, ...]
    Returns {"canonical": id, "duplicates": [ids], "skip": reason|None}.
    """
    if not rows or len(rows) < 2:
        return {"canonical": None, "duplicates": [], "skip": "single_row"}

    named = [r for r in rows if not is_anonymous(r.get("provider"))]
    anon = [r for r in rows if is_anonymous(r.get("provider"))]

    # ★ THE SAFETY RULE. Two distinct real operators in one (name, city) group
    # means distinct buildings sharing a generic name — the IAD85/IAD75/IAD96
    # case. Never merge those.
    distinct_named = {(r.get("provider") or "").strip().lower() for r in named}
    if len(distinct_named) > 1:
        return {"canonical": None, "duplicates": [],
                "skip": "multiple_real_providers"}
    if not named:
        return {"canonical": None, "duplicates": [], "skip": "no_named_row"}
    if not anon:
        return {"canonical": None, "duplicates": [], "skip": "no_anonymous_row"}

    # ★ Coordinates VETO a merge; they never justify one. Rows without coords do
    # not block (a missing coordinate is not evidence of distance), but any two
    # KNOWN coordinates further than _COORD_EPS apart stop the group.
    lats = [r["latitude"] for r in rows if r.get("latitude") is not None]
    lons = [r["longitude"] for r in rows if r.get("longitude") is not None]
    if lats and (max(lats) - min(lats)) > _COORD_EPS:
        return {"canonical": None, "duplicates": [], "skip": "coords_far_apart"}
    if lons and (max(lons) - min(lons)) > _COORD_EPS:
        return {"canonical": None, "duplicates": [], "skip": "coords_far_apart"}

    # The canonical must be able to RECEIVE the redirect/canonical tag, so it
    # needs a real frozen slug. Richest row wins ties (power_mw), then lowest id
    # for determinism — the same group must always plan the same way.
    usable = [r for r in named if (r.get("canonical_slug") or "").strip()]
    if not usable:
        return {"canonical": None, "duplicates": [],
                "skip": "named_row_has_no_slug"}
    usable.sort(key=lambda r: (-(r.get("power_mw") or 0), r["id"]))
    canonical = usable[0]

    dups = [r["id"] for r in anon if r["id"] != canonical["id"]]
    if not dups:
        return {"canonical": None, "duplicates": [], "skip": "nothing_to_mark"}
    return {"canonical": canonical["id"], "duplicates": dups, "skip": None}


# ★`duplicate_of_id` is SELECTED but deliberately NOT in the WHERE. This lane's
# only output is that column, and the gate above tests `is_duplicate`, which v3
# never writes (see apply) — so without reading the pointer the scan cannot tell
# a row it has already marked from one it has not, and reports its own output as
# outstanding work forever. Filtering it in the WHERE would be a different
# change: it would drop already-pointed rows out of their GROUPS, which alters
# canonical election and what gets written. Reading it and subtracting in Python
# leaves grouping byte-identical and only corrects the arithmetic.
_GROUP_SQL = """
    SELECT lower(trim(name)) AS nm,
           lower(trim(coalesce(city,''))) AS ci,
           id, provider, canonical_slug, latitude, longitude, power_mw,
           duplicate_of_id
      FROM discovered_facilities
     WHERE COALESCE(is_duplicate, 0) = 0
       AND canonical_slug IS NOT NULL AND canonical_slug <> ''
       AND name IS NOT NULL AND trim(name) <> ''
       {country}
     ORDER BY nm, ci, id
"""


def _collect(cur, country=None, limit=None):
    """Group live rows by (name, city) and plan each. Returns (plans, stats).

    ★A PLAN COUNTS ONLY ROWS APPLY CAN ACTUALLY MARK. `apply`'s UPDATE carries
    `AND duplicate_of_id IS NULL`, so a row this lane has already pointed is not
    work — but the scan's own gate (`COALESCE(is_duplicate,0)=0`) cannot see
    that, because v3 deliberately never writes `is_duplicate`. Before this
    subtraction /analyze re-reported every row it had ever marked: measured live
    2026-08-16, `groups_planned: 671` and `urls_removed: 675` when apply would
    mark 58 — a 12x over-report, 617 phantom rows, and 614 of the 671 groups
    could only ever issue an UPDATE matching nothing.

    ★This is arithmetic only. Grouping and canonical election are unchanged: the
    already-pointed rows still take part in their group, so `plan_group` sees
    exactly the rows it saw before and returns exactly the same verdict. What
    changes is that a group with nothing left to mark stops being counted as
    outstanding work, and apply stops issuing its no-op UPDATE.

    ★A group whose CANONICAL is itself already pointed still plans, exactly as
    before — that is a pointer-chain question, not a counting one, and fixing it
    would change what the deduper writes. Deliberately out of scope here.
    """
    params = []
    country_clause = ""
    if country:
        country_clause = "AND upper(coalesce(country,'')) = %s"
        params.append(country.strip().upper())
    cur.execute(_GROUP_SQL.format(country=country_clause), tuple(params))

    groups, cur_key, bucket = [], None, []
    for r in cur.fetchall() or []:
        key = (r[0], r[1])
        row = {"id": r[2], "provider": r[3], "canonical_slug": r[4],
               "latitude": r[5], "longitude": r[6], "power_mw": r[7],
               "duplicate_of_id": r[8]}
        if key != cur_key:
            if bucket:
                groups.append((cur_key, bucket))
            cur_key, bucket = key, [row]
        else:
            bucket.append(row)
    if bucket:
        groups.append((cur_key, bucket))

    plans, stats = [], {}
    for key, rows in groups:
        if len(rows) < 2:
            continue
        p = plan_group(rows)
        if p["skip"]:
            stats[p["skip"]] = stats.get(p["skip"], 0) + 1
            continue
        # ★ subtract this lane's own output — see the docstring. The set is built
        # from the GROUP's rows, so it costs no extra query.
        pointed = {r["id"] for r in rows if r.get("duplicate_of_id") is not None}
        todo = [i for i in p["duplicates"] if i not in pointed]
        if not todo:
            stats["already_pointed"] = stats.get("already_pointed", 0) + 1
            continue
        stats["planned"] = stats.get("planned", 0) + 1
        plans.append({"name": key[0], "city": key[1],
                      "canonical": p["canonical"], "duplicates": todo})
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


@facility_dedup_v3_bp.route("/api/v1/admin/facility-dedup-v3/analyze")
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
            plans, stats = _collect(cur, request.args.get("country"),
                                    int(request.args.get("limit") or 0) or None)
        return jsonify(ok=True, dry_run=True, method=DEDUP_METHOD,
                       mode="pointer_only (never sets is_duplicate)",
                       write_conn=_write_diag(),
                       groups_planned=len(plans),
                       urls_removed=sum(len(p["duplicates"]) for p in plans),
                       skipped=stats, sample=plans[:20]), 200
    except Exception as e:
        logger.warning("facility_dedup_v3 analyze failed: %s", e)
        return jsonify(ok=False, error=str(e)[:300]), 500
    finally:
        try: conn.close()
        except Exception: pass


@facility_dedup_v3_bp.route("/api/v1/admin/facility-dedup-v3/apply",
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
            plans, stats = _collect(cur, request.args.get("country"),
                                    int(request.args.get("limit") or 0) or None)
            for p in plans:
                # ★ The WHERE re-asserts every precondition at WRITE time: a row
                # another pass flagged between analyze and apply is left alone,
                # and a row can never be made its own duplicate.
                # ★★ POINTER-ONLY. is_duplicate is DELIBERATELY NOT WRITTEN.
                # The first apply set is_duplicate=1 and left 57 of 58 slugs
                # with no surviving keeper — the bug
                # repair_dedup_keeper_election.py fixed on 2026-07-27. It was
                # reverted. is_duplicate is a VISIBILITY flag: it drops the row
                # from every filtered count and from the sitemap, and buys
                # nothing a canonical does not already buy.
                # A duplicate_of_id alone makes the page emit rel=canonical at
                # its twin (routes/facility_profile_page.py) while the row stays
                # live, counted and serving 200. Verified on a single pair:
                # unknown-brainserve-dd3fd3d9 and brainserve-1d8a2e5a both
                # canonicalise to brainserve-sa-brainserve-a3c43931, all three
                # still HTTP 200, live count and no-keeper count both unchanged.
                # ★ duplicate_of_id IS NULL replaces the old dedup_method IS NULL
                #   guard. The old one blocked 375 rows because v2 had merely
                #   EXAMINED them (stamped, nothing pointing at them, no
                #   decision). What must never be overwritten is an existing
                #   POINTER — another pass's actual verdict.
                # ★ The v2 stamp on such a row records "examined, no cluster";
                #   v3 is deciding where v2 did not, so the method is replaced.
                #   undo returns it to NULL.
                cur.execute(
                    "UPDATE discovered_facilities "
                    "   SET duplicate_of_id = %s, dedup_method = %s "
                    " WHERE id = ANY(%s) "
                    "   AND duplicate_of_id IS NULL "
                    "   AND COALESCE(is_duplicate, 0) = 0 "
                    "   AND id <> %s",
                    (p["canonical"], DEDUP_METHOD, p["duplicates"],
                     p["canonical"]))
                marked += cur.rowcount or 0
        return jsonify(ok=True, applied=True, method=DEDUP_METHOD,
                       groups=len(plans), rows_marked=marked,
                       skipped=stats,
                       mode="pointer_only (never sets is_duplicate)",
                       note="reversible: POST .../facility-dedup-v3/undo?confirm=1"), 200
    except Exception as e:
        logger.warning("facility_dedup_v3 apply failed: %s", e)
        return jsonify(ok=False, error=str(e)[:300], rows_marked=marked), 500
    finally:
        try: conn.close()
        except Exception: pass


@facility_dedup_v3_bp.route("/api/v1/admin/facility-dedup-v3/undo",
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
            # ★ dedup_method = THIS module's stamp only. A v3 undo must never
            # roll back a v2 decision.
            # ★ Clears ONLY what apply wrote — the pointer and the stamp.
            # is_duplicate is not touched on the way out either: this module
            # never sets it, so "restoring" it would be writing a value we did
            # not change. The is_duplicate=0 predicate also means a row some
            # OTHER pass later suppressed is left alone.
            cur.execute(
                "UPDATE discovered_facilities "
                "   SET duplicate_of_id = NULL, dedup_method = NULL "
                " WHERE dedup_method = %s "
                "   AND COALESCE(is_duplicate, 0) = 0", (DEDUP_METHOD,))
            n = cur.rowcount or 0
        return jsonify(ok=True, rows_cleared=n, method=DEDUP_METHOD), 200
    except Exception as e:
        logger.warning("facility_dedup_v3 undo failed: %s", e)
        return jsonify(ok=False, error=str(e)[:300]), 500
    finally:
        try: conn.close()
        except Exception: pass
