"""Phase FF+25-followup-r14 (2026-05-20) — facility admin endpoint.
==========================================================================

User found a coverage gap: DCHawk has two Calgary-metro facilities
(Gryphon Digital Mining in Pincher Creek; Prairie Sky in Strathmore)
that we don't. Manual insert plus a coverage-gap brain detector so the
brain catches these autonomously going forward.

  POST /api/v1/admin/facilities/add    add a single facility (idempotent
                                        via ON CONFLICT on source_id)
  POST /api/v1/admin/facilities/bulk   add multiple at once
  GET  /api/v1/admin/facilities/recent  list recently-added manual facilities
"""
import os
import json
import hashlib
import logging
import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
facility_admin_bp = Blueprint("facility_admin", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _insert_one(cur, f: dict) -> tuple[bool, str]:
    """Insert one facility row into BOTH facilities (canonical merged
    table) AND discovered_facilities (the public-search index).

    FIX r17 (2026-05-20): user reported the seeded Canadian rows
    weren't searchable via /api/v1/facilities. Root cause: the public
    free-tier search hits discovered_facilities, not facilities, but
    this endpoint only inserted into facilities. Visitors couldn't
    find the rows we'd added.

    Now we write to both tables in the same transaction, link the
    discovered row to the canonical row via merged_facility_id, and
    set confidence_score=1.0 since manual entries are operator-
    verified (vs crawler-found rows which start at 0.5).

    FIX r14b: facilities table has no UNIQUE constraint on source_id,
    so ON CONFLICT (source_id) was raising. SELECT-then-INSERT instead."""
    name = (f.get("name") or "").strip()
    if not name:
        return False, ""
    source_id = ("manual_"
                 + hashlib.sha256(name.encode()).hexdigest()[:16])

    # Already exists in canonical table?
    try:
        cur.execute(
            "SELECT 1 FROM facilities WHERE source_id = %s LIMIT 1",
            (source_id,),
        )
        already_canonical = cur.fetchone() is not None
    except Exception:
        already_canonical = False

    if not already_canonical:
        cur.execute("""
            INSERT INTO facilities (
                id, name, provider, city, state, country, power_mw,
                status, address, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s)
        """, (
            source_id, name, f.get("provider"),
            f.get("city"), f.get("state") or f.get("province"),
            f.get("country", "US"),
            float(f.get("power_mw", 0) or 0),
            f.get("status", "planned"),
            f.get("address"), source_id,
        ))

    # Also stage into discovered_facilities so the public free-tier
    # search surface sees the row. UNIQUE(source, source_id) makes this
    # idempotent — re-running is a no-op via ON CONFLICT.
    try:
        cur.execute("""
            INSERT INTO discovered_facilities (
                source, source_id, name, provider, city, state, country,
                power_mw, status, address, confidence_score,
                is_duplicate, merged_facility_id, discovered_at,
                first_seen, last_updated
            )
            VALUES ('manual', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    1.0, 0, %s, NOW()::TEXT, NOW()::TEXT, NOW()::TEXT)
            ON CONFLICT (source, source_id) DO UPDATE SET
                name = EXCLUDED.name,
                provider = EXCLUDED.provider,
                city = EXCLUDED.city,
                state = EXCLUDED.state,
                country = EXCLUDED.country,
                power_mw = EXCLUDED.power_mw,
                status = EXCLUDED.status,
                address = EXCLUDED.address,
                last_updated = NOW()::TEXT
        """, (
            source_id, name, f.get("provider"),
            f.get("city"), f.get("state") or f.get("province"),
            f.get("country", "US"),
            float(f.get("power_mw", 0) or 0),
            f.get("status", "planned"),
            f.get("address"), source_id,
        ))
    except Exception as e:
        # discovered_facilities may not have the expected schema on
        # every deploy; don't let it prevent the canonical insert.
        logger.warning(f"[facility-admin] discovered_facilities "
                       f"insert/update skipped: {str(e)[:150]}")

    return (not already_canonical, source_id)


@facility_admin_bp.route("/api/v1/admin/facilities/add", methods=["POST"])
def add_facility():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    f = request.get_json(silent=True) or {}
    if not (f.get("name")):
        return jsonify(ok=False, error="name_required"), 400
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            added, sid = _insert_one(cur, f)
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, added=added, source_id=sid,
                       name=f.get("name"))
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@facility_admin_bp.route("/api/v1/admin/facilities/bulk", methods=["POST"])
def add_facilities_bulk():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    body = request.get_json(silent=True) or {}
    rows = body.get("facilities") or body.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return jsonify(ok=False, error="facilities list required"), 400
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    added = 0
    failed = []
    try:
        with c.cursor() as cur:
            for f in rows:
                try:
                    ok, _sid = _insert_one(cur, f)
                    if ok: added += 1
                except Exception as e:
                    failed.append({"name": f.get("name"), "error": str(e)[:120]})
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, attempted=len(rows), added=added,
                       failed=failed)
    finally:
        try: c.close()
        except Exception: pass


@facility_admin_bp.route("/api/v1/admin/facilities/backfill-discovered",
                          methods=["POST"])
def backfill_discovered():
    """One-shot: copy all rows where source='manual' from the
    facilities canonical table into discovered_facilities so the
    public free-tier search can surface them. Idempotent — re-running
    is a no-op for rows that already exist via the UNIQUE(source,
    source_id) constraint.

    Use this after r17 to fix any manual rows that were inserted
    before this commit and never got staged into the search index."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    staged = 0
    skipped = 0
    errors = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT name, provider, city, state, country, power_mw,
                       status, address, source_id
                  FROM facilities
                 WHERE source = 'manual'
                 ORDER BY id ASC
            """)
            rows = cur.fetchall()
            for r in rows:
                try:
                    cur.execute("""
                        INSERT INTO discovered_facilities (
                            source, source_id, name, provider, city, state,
                            country, power_mw, status, address,
                            confidence_score, is_duplicate,
                            merged_facility_id, discovered_at,
                            first_seen, last_updated
                        )
                        VALUES ('manual', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                1.0, 0, %s, NOW()::TEXT,
                                NOW()::TEXT, NOW()::TEXT)
                        ON CONFLICT (source, source_id) DO NOTHING
                    """, (r[8], r[0], r[1], r[2], r[3], r[4],
                           float(r[5] or 0), r[6], r[7], r[8]))
                    if cur.rowcount > 0:
                        staged += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors.append({"source_id": r[8], "error": str(e)[:120]})
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, total=len(rows),
                       staged=staged, skipped_existing=skipped,
                       errors=errors[:10])
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# ── Phase r23: dedicated key-deactivate endpoint ─────────────────────
# Used for cleaning up dual-key cases (Kevin had 2 keys for 1
# subscription due to the pre-r20 bug). Deactivates a specific key by
# id and leaves the other keys for the same user alone.
@facility_admin_bp.route("/api/v1/admin/keys/deactivate",
                          methods=["POST"])
def deactivate_key():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    p = request.get_json(silent=True) or {}
    key_id = p.get("key_id") or request.args.get("key_id")
    if not key_id:
        return jsonify(ok=False, error="key_id_required"), 400
    try:
        key_id = int(key_id)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="key_id_must_be_int"), 400
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET is_active = 0 "
                "WHERE id = %s "
                "RETURNING id, key_prefix, name, plan",
                (key_id,),
            )
            r = cur.fetchone()
        try: c.commit()
        except Exception: pass
        if not r:
            return jsonify(ok=False, error="key_not_found",
                           key_id=key_id), 404
        return jsonify(
            ok=True, deactivated=True,
            key_id=int(r[0]),
            key_prefix=r[1],
            name=r[2],
            plan=r[3],
        )
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@facility_admin_bp.route("/api/v1/admin/facilities/recent",
                          methods=["GET"])
def recent_manual():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT name, provider, city, state, country, power_mw,
                       status, source_id
                  FROM facilities
                 WHERE source = 'manual'
                 ORDER BY id DESC LIMIT 50
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "name": r[0], "provider": r[1], "city": r[2],
                    "state_or_province": r[3], "country": r[4],
                    "power_mw": float(r[5] or 0), "status": r[6],
                    "source_id": r[7],
                })
        return jsonify(ok=True, count=len(rows), facilities=rows)
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info("[facility-admin] ready · POST /api/v1/admin/facilities/"
                 "add|bulk · GET /recent")

_smoke()
