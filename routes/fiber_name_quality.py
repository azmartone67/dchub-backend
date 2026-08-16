"""fiber_name_quality.py — scrub HIFLD sentinels out of fiber_routes (2026-08-15).

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

THE SIBLING COLUMN (2026-08-16)
-------------------------------
The name lane above ran live and reports `fixable: 0`. `fiber_routes.provider`
was left dirty by that scoping and holds the SAME owner sentinel on 1,150 rows,
all `source='hifld'`. It is served publicly as `carrier` on /api/v1/fiber/routes
and it is FILTERABLE, so this half is NOT cosmetic:

    GET /api/v1/fiber/routes?carrier=NOT%20AVAILABLE   ->   total: 1150

a query result that publishes a database artifact as if it were a carrier you
could buy from. The provider lane repairs it to whatever
`infrastructure_discovery.hifld_owner` calls "we don't know" — mapped THROUGH
that function, never through a second copy of the sentinel list, so the backfill
and the ingest can never drift apart. The ingest side is already correct
(hifld_owner is case-insensitive and catches the live 'NOT AVAILABLE'); this is
purely a backfill of rows written before that landed.

★UNLIKE THE NAME LANE, THIS ONE CHANGES QUERY RESULTS. `?carrier=NOT AVAILABLE`
goes to 0 rows, and any COUNT(DISTINCT provider) drops by one as the two
"unknown" spellings merge into the one the ingest now writes. That is the point
of the repair, not a side effect of it — but it is why the lane is separate,
separately confirmed, and separately undoable.

★UNIQUE(name, provider) IS LIVE — twice (fiber_routes_name_provider_key and
fiber_routes_name_provider_unique). Collapsing two provider spellings into one
can therefore collide with a row that already holds the repaired value, so a
repair is SKIPPED (not attempted and not swallowed) when the target key is
taken, and analyze/apply report the count. Measured live 2026-08-16: 0 rows
collide. The guard exists because the arithmetic can change under a re-run, not
because it fires today.

SHAPE — the same analyze / apply / undo contract as facility_geo_quality:
  GET  /api/v1/admin/fiber-names/analyze              dry run, counts + sample
  POST /api/v1/admin/fiber-names/apply?confirm=1      rewrite (no confirm => dry run)
  POST /api/v1/admin/fiber-names/undo?confirm=1       restore from name_orig
  GET  /api/v1/admin/fiber-providers/analyze          dry run, counts + sample
  POST /api/v1/admin/fiber-providers/apply?confirm=1  rewrite (no confirm => dry run)
  POST /api/v1/admin/fiber-providers/undo?confirm=1   restore from provider_orig

★The original value is preserved in `name_orig` / `provider_orig` before the
first write, and `apply` NEVER overwrites a non-null *_orig. That is what makes
undo total: re-running apply cannot launder a repaired value into the
"original" slot.

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
            # Both lanes' undo slots are created together — ADD COLUMN IF NOT
            # EXISTS is idempotent, and a missing undo column is the one failure
            # that would make a repair unreversible.
            cur.execute("ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS provider_orig TEXT")
            cur.execute("ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS provider_fixed_at TIMESTAMPTZ")
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


# ── the provider lane (served publicly as `carrier`) ─────────────────────────
# Everything below maps the sentinel family THROUGH infrastructure_discovery.
# There is deliberately no list of sentinel strings in this file: the ingest
# owns that set, and a second copy here would drift the day someone adds a
# sentinel to one of them. Imported lazily — this module is a Flask blueprint
# imported at boot, and infrastructure_discovery pulls requests + db_utils.

def _hifld_owner():
    """`infrastructure_discovery.hifld_owner`, the one definition of the family."""
    from infrastructure_discovery import hifld_owner
    return hifld_owner


def _unknown_owner():
    """Whatever the ingest calls "we don't know", asked rather than assumed.

    ★Derived by calling hifld_owner with nothing, so the literal 'Unknown' never
    appears here. If the ingest ever renames its fallback, this follows.
    """
    return _hifld_owner()("")


def _sentinel_values():
    """The non-blank sentinel spellings, lowercased — the SQL prefilter only.

    Blanks are excluded on purpose: `provider = ''` is not a sentinel this
    backfill has any business inventing a value for (and live has none). The
    authority on what actually changes is repair_provider, exactly as the name
    lane trusts repair_name over its ILIKE.
    """
    from infrastructure_discovery import _HIFLD_NULL_STRINGS
    return sorted(s for s in _HIFLD_NULL_STRINGS if s and s.strip())


def repair_provider(provider):
    """Pure. A HIFLD owner sentinel becomes the ingest's fallback; anything else
    comes back byte-identical.

    'NOT AVAILABLE' -> 'Unknown';  'Zayo' -> 'Zayo';  '' / None -> unchanged.

    ★Returns the input unchanged when there is nothing to do, so the caller can
    use identity as the did-anything test rather than trusting a rowcount.
    ★hifld_owner also strips whitespace off a REAL owner name; that is not this
    backfill's business, so only a mapping to the fallback counts as a repair.
    """
    if provider is None or not str(provider).strip():
        return provider
    fallback = _unknown_owner()
    if _hifld_owner()(provider) != fallback:
        return provider
    return provider if str(provider) == fallback else fallback


def _defer_intra_set_collisions(fixable, collisions):
    """Pure. Split off candidates that would collide with EACH OTHER.

    fiber_routes carries a live UNIQUE(name, provider) (twice over). The SQL
    EXISTS in _scan_providers reads the pre-UPDATE snapshot, so it can see a row
    that ALREADY holds the repaired provider but never a sibling candidate that
    is about to. Two rows sharing a name cannot both hold one sentinel spelling
    (that is what the constraint means), but they CAN hold two different ones —
    and both repair to the same fallback. Keep the lowest id of each such group
    and defer the rest rather than let one collision fail a whole chunk.
    """
    seen = set()
    keep, deferred = [], list(collisions)
    for r in fixable:
        key = (r["name"], r["to"])
        if key in seen:
            deferred.append(r)
        else:
            seen.add(key)
            keep.append(r)
    return keep, deferred


def _scan_providers(limit=None):
    """(fixable, collisions) — rows whose provider is a sentinel, split by
    whether the repaired (name, provider) key is already taken."""
    c = _conn()
    if c is None:
        return None
    fallback = _unknown_owner()
    fixable, collisions = [], []
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT f.id, f.name, f.provider, EXISTS ("
                "  SELECT 1 FROM fiber_routes g WHERE g.id <> f.id"
                "   AND g.name IS NOT DISTINCT FROM f.name AND g.provider = %s) "
                "FROM fiber_routes f "
                "WHERE btrim(lower(coalesce(f.provider, ''))) = ANY(%s) "
                "ORDER BY f.id" + (" LIMIT %d" % int(limit) if limit else ""),
                (fallback, _sentinel_values()))
            for rid, name, provider, key_taken in cur.fetchall() or []:
                fixed = repair_provider(provider)
                if fixed == provider:
                    continue
                rec = {"id": rid, "name": name, "from": provider, "to": fixed}
                (collisions if key_taken else fixable).append(rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("[fiber-providers] scan failed: %s", str(e)[:160])
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass
    return _defer_intra_set_collisions(fixable, collisions)


# One statement per chunk instead of one per row. The name lane's per-row loop
# took 53.9s for 1,674 rows, which is longer than the edge's default route
# timeout — this lane does not inherit that.
_APPLY_CHUNK = 500


@fiber_name_quality_bp.route("/api/v1/admin/fiber-providers/analyze")
def fiber_providers_analyze():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    scanned = _scan_providers()
    if scanned is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    rows, collisions = scanned
    return jsonify(ok=True, dry_run=True, fixable=len(rows),
                   collisions=len(collisions),
                   scope="fiber_routes.provider — served publicly as `carrier` "
                         "on /api/v1/fiber/routes, and filterable",
                   note="collisions are rows whose repaired (name, provider) is "
                        "already held by another row under the live "
                        "UNIQUE(name, provider); they are skipped, not failed",
                   sample=rows[:25], collision_sample=collisions[:10]), 200


@fiber_name_quality_bp.route("/api/v1/admin/fiber-providers/apply", methods=["POST"])
def fiber_providers_apply():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    scanned = _scan_providers()
    if scanned is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    rows, collisions = scanned
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True, would_fix=len(rows),
                       collisions=len(collisions),
                       note="add ?confirm=1 to rewrite"), 200
    _ensure_columns()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    fallback = _unknown_owner()
    fixed = 0
    try:
        with c.cursor() as cur:
            for i in range(0, len(rows), _APPLY_CHUNK):
                ids = [r["id"] for r in rows[i:i + _APPLY_CHUNK]]
                # COALESCE keeps the FIRST original forever — re-running apply
                # can never launder a repaired provider into the undo slot.
                # The NOT EXISTS re-checks the collision the scan already
                # excluded, so a row that became conflicting between scan and
                # write is skipped ATOMICALLY (rowcount 0) instead of raising
                # and aborting the rest of the chunk.
                cur.execute(
                    "UPDATE fiber_routes f SET "
                    "  provider_orig = COALESCE(f.provider_orig, f.provider), "
                    "  provider = %s, provider_fixed_at = NOW() "
                    "WHERE f.id = ANY(%s) AND NOT EXISTS ("
                    "  SELECT 1 FROM fiber_routes g WHERE g.id <> f.id"
                    "   AND g.name IS NOT DISTINCT FROM f.name"
                    "   AND g.provider = %s)",
                    (fallback, ids, fallback))
                fixed += cur.rowcount or 0
    except Exception as e:  # noqa: BLE001
        logger.warning("[fiber-providers] apply failed: %s", str(e)[:160])
        return jsonify(ok=False, error="apply_failed", detail=str(e)[:200],
                       fixed_before_error=fixed), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    # skipped = selected for repair but not written, i.e. a key taken between
    # the scan and the write. Reported rather than inferred from a silent gap.
    return jsonify(ok=True, dry_run=False, fixed=fixed,
                   collisions=len(collisions),
                   skipped=len(rows) - fixed), 200


@fiber_name_quality_bp.route("/api/v1/admin/fiber-providers/undo", methods=["POST"])
def fiber_providers_undo():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True, note="add ?confirm=1 to restore"), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    try:
        with c.cursor() as cur:
            # Restoring cannot collide: it puts back exactly the key set that
            # was live before apply, and the fixed ingest no longer writes any
            # sentinel spelling that could have taken one since.
            cur.execute("UPDATE fiber_routes SET provider = provider_orig, "
                        "provider_orig = NULL, provider_fixed_at = NULL "
                        "WHERE provider_orig IS NOT NULL")
            n = cur.rowcount or 0
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error="undo_failed", detail=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify(ok=True, restored=n), 200
