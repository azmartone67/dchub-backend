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


def _conn():
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


_GROUP_SQL = """
    SELECT lower(trim(name)) AS nm,
           lower(trim(coalesce(city,''))) AS ci,
           id, provider, canonical_slug, latitude, longitude, power_mw
      FROM discovered_facilities
     WHERE COALESCE(is_duplicate, 0) = 0
       AND canonical_slug IS NOT NULL AND canonical_slug <> ''
       AND name IS NOT NULL AND trim(name) <> ''
       {country}
     ORDER BY nm, ci, id
"""


def _collect(cur, country=None, limit=None):
    """Group live rows by (name, city) and plan each. Returns (plans, stats)."""
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
               "latitude": r[5], "longitude": r[6], "power_mw": r[7]}
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
        stats[p["skip"] or "planned"] = stats.get(p["skip"] or "planned", 0) + 1
        if p["skip"]:
            continue
        plans.append({"name": key[0], "city": key[1],
                      "canonical": p["canonical"], "duplicates": p["duplicates"]})
        if limit and len(plans) >= limit:
            break
    return plans, stats


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
    conn = _conn()
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
                cur.execute(
                    "UPDATE discovered_facilities "
                    "   SET is_duplicate = 1, duplicate_of_id = %s, "
                    "       dedup_method = %s "
                    " WHERE id = ANY(%s) "
                    "   AND COALESCE(is_duplicate, 0) = 0 "
                    "   AND dedup_method IS NULL "
                    "   AND id <> %s",
                    (p["canonical"], DEDUP_METHOD, p["duplicates"],
                     p["canonical"]))
                marked += cur.rowcount or 0
        return jsonify(ok=True, applied=True, method=DEDUP_METHOD,
                       groups=len(plans), rows_marked=marked,
                       skipped=stats,
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
    conn = _conn()
    if conn is None:
        return jsonify(ok=False, error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            # ★ dedup_method = THIS module's stamp only. A v3 undo must never
            # roll back a v2 decision.
            cur.execute(
                "UPDATE discovered_facilities "
                "   SET is_duplicate = 0, duplicate_of_id = NULL, "
                "       dedup_method = NULL "
                " WHERE dedup_method = %s", (DEDUP_METHOD,))
            n = cur.rowcount or 0
        return jsonify(ok=True, rows_cleared=n, method=DEDUP_METHOD), 200
    except Exception as e:
        logger.warning("facility_dedup_v3 undo failed: %s", e)
        return jsonify(ok=False, error=str(e)[:300]), 500
    finally:
        try: conn.close()
        except Exception: pass
