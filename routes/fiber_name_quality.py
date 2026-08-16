"""fiber_name_quality.py — scrub HIFLD sentinels out of fiber_routes.name (2026-08-15).

THE DEFECT
----------
718 rows in `fiber_routes` are named like:

    NOT AVAILABLE -999999kV Line - Columbus [6faf59c85f0e]

Both halves are HIFLD "we don't know" sentinels that were never scrubbed:
`-999999` for VOLTAGE and the literal string `NOT AVAILABLE` for OWNER. The
ingest guard read `attrs.get('VOLTAGE', 0) or 0`, which looks like a null-check
and is not one — `-999999` is truthy, so the `or 0` never fires. The source is
fixed in infrastructure_discovery.hifld_voltage / hifld_owner / hifld_line_name;
this module repairs the rows already written.

★DISPLAY NAMES ONLY. `_save_route` persists no voltage column, so nothing
numeric is poisoned and no query result changes. This is a cosmetic repair and
is deliberately scoped as one — but these names are served publicly on
/api/v1/fiber/routes, so "cosmetic" means "wrong in front of customers".

SHAPE — the same analyze / apply / undo contract as facility_geo_quality:
  GET  /api/v1/admin/fiber-names/analyze          dry run, counts + sample
  POST /api/v1/admin/fiber-names/apply?confirm=1  rewrite (no confirm => dry run)
  POST /api/v1/admin/fiber-names/undo?confirm=1   restore from name_orig

★The original name is preserved in `name_orig` before the first write, and
`apply` NEVER overwrites a non-null `name_orig`. That is what makes undo total:
re-running apply cannot launder a repaired name into the "original" slot.

Auth: X-Admin-Key / ?admin_key= (DCHUB_ADMIN_KEY). Read-only until ?confirm=1.
"""
from __future__ import annotations

import logging
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
fiber_name_quality_bp = Blueprint("fiber_name_quality", __name__)

# The sentinel shapes actually observed in the live table. Anchored on the
# literals rather than "any negative number" so a legitimately odd name can
# never be caught by this.
_SENTINEL_SQL = "(name ILIKE %s OR name ILIKE %s)"
_SENTINEL_ARGS = ("%-999999kV%", "NOT AVAILABLE %")

_VOLT_RE = re.compile(r"\s*-99999[89]kV", re.I)
_OWNER_RE = re.compile(r"^\s*(NOT AVAILABLE|UNKNOWN|N/A|NO DATA)\s+", re.I)


def repair_name(name: str) -> str:
    """Pure. Drop the sentinel fragments; leave everything else byte-identical.

    'NOT AVAILABLE -999999kV Line - Columbus [6faf]' -> 'Unknown Line - Columbus [6faf]'

    ★Returns the input unchanged when there is nothing to strip, so the caller
    can use identity as the did-anything test rather than trusting a rowcount.
    """
    if not name:
        return name
    out = _VOLT_RE.sub("", str(name))
    out = _OWNER_RE.sub("Unknown ", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out or str(name)


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
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    return bool(expected) and provided == expected


def _ensure_columns():
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS name_orig TEXT")
            cur.execute("ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS name_fixed_at TIMESTAMPTZ")
    except Exception as e:  # noqa: BLE001
        logger.warning("[fiber-names] ensure columns failed: %s", str(e)[:140])
    finally:
        try:
            c.close()
        except Exception:
            pass


def _scan(limit=None):
    """Rows whose name carries a sentinel, with the repair each would get."""
    c = _conn()
    if c is None:
        return None
    rows = []
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, name, provider FROM fiber_routes WHERE " + _SENTINEL_SQL
                + " ORDER BY id" + (" LIMIT %s" % int(limit) if limit else ""),
                _SENTINEL_ARGS)
            for rid, name, provider in cur.fetchall() or []:
                fixed = repair_name(name)
                if fixed != name:
                    rows.append({"id": rid, "from": name, "to": fixed,
                                 "provider": provider})
    except Exception as e:  # noqa: BLE001
        logger.warning("[fiber-names] scan failed: %s", str(e)[:160])
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass
    return rows


@fiber_name_quality_bp.route("/api/v1/admin/fiber-names/analyze")
def fiber_names_analyze():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    rows = _scan()
    if rows is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    return jsonify(ok=True, dry_run=True, fixable=len(rows),
                   scope="display names only — no numeric column is affected",
                   sample=rows[:25]), 200


@fiber_name_quality_bp.route("/api/v1/admin/fiber-names/apply", methods=["POST"])
def fiber_names_apply():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    rows = _scan()
    if rows is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True, would_fix=len(rows),
                       note="add ?confirm=1 to rewrite"), 200
    _ensure_columns()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    fixed = 0
    try:
        with c.cursor() as cur:
            for r in rows:
                # COALESCE keeps the FIRST original forever — re-running apply
                # can never launder a repaired name into the undo slot.
                cur.execute(
                    "UPDATE fiber_routes SET name_orig = COALESCE(name_orig, name), "
                    "name = %s, name_fixed_at = NOW() WHERE id = %s",
                    (r["to"], r["id"]))
                fixed += cur.rowcount or 0
    except Exception as e:  # noqa: BLE001
        logger.warning("[fiber-names] apply failed: %s", str(e)[:160])
        return jsonify(ok=False, error="apply_failed", detail=str(e)[:200],
                       fixed_before_error=fixed), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify(ok=True, dry_run=False, fixed=fixed), 200


@fiber_name_quality_bp.route("/api/v1/admin/fiber-names/undo", methods=["POST"])
def fiber_names_undo():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True, note="add ?confirm=1 to restore"), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    try:
        with c.cursor() as cur:
            cur.execute("UPDATE fiber_routes SET name = name_orig, name_orig = NULL, "
                        "name_fixed_at = NULL WHERE name_orig IS NOT NULL")
            n = cur.rowcount or 0
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error="undo_failed", detail=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify(ok=True, restored=n), 200
