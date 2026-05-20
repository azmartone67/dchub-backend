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
    """Insert one facility row. Returns (added, source_id).
    FIX r14b: facilities table has no UNIQUE constraint on source_id,
    so ON CONFLICT (source_id) was raising. Doing a SELECT-then-INSERT
    pattern instead — idempotency at the application layer."""
    name = (f.get("name") or "").strip()
    if not name:
        return False, ""
    source_id = ("manual_"
                 + hashlib.sha256(name.encode()).hexdigest()[:16])
    # Already exists?
    try:
        cur.execute(
            "SELECT 1 FROM facilities WHERE source_id = %s LIMIT 1",
            (source_id,),
        )
        if cur.fetchone():
            return False, source_id   # already present, no-op
    except Exception:
        pass
    cur.execute("""
        INSERT INTO facilities (
            id, name, provider, city, state, country, power_mw,
            status, address, source, source_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s)
        RETURNING id
    """, (
        source_id, name, f.get("provider"),
        f.get("city"), f.get("state") or f.get("province"),
        f.get("country", "US"),
        float(f.get("power_mw", 0) or 0),
        f.get("status", "planned"),
        f.get("address"), source_id,
    ))
    r = cur.fetchone()
    return (bool(r), source_id)


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
