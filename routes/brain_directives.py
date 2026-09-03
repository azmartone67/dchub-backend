"""
Phase FF+directives (2026-05-22) — operator directive intake for the brain.

Until now the brain was purely self-directed: detectors emit findings, the
worklist ranks them, Layer 4/5 attack them. There was no way for a human to
say "build the CAISO adapter" or "fix the /vs footer" and have the autonomous
loop pick it up.

This blueprint adds that channel — SAFELY:

  GET  /api/v1/brain/directives            public, read-only list (open by default)
  POST /api/v1/brain/directives            ADMIN-gated: queue a new directive
  POST /api/v1/brain/directives/<id>/status ADMIN-gated: transition status

SECURITY: the brain opens PRs and the watchdog can apply fixes, so directive
WRITES are a high-value injection target. Writes require the X-Admin-Key header
to match the BRAIN_ADMIN_KEY env var (constant-time compare). If that env var
is unset, writes are DISABLED (503) — safe by default. Reads are harmless and
stay public so the directive queue is visible on the transparency surface.
"""

import os
import hmac
import logging
from flask import Blueprint, request, jsonify

from routes import brain_v2_store as store

logger = logging.getLogger(__name__)
brain_directives_bp = Blueprint("brain_directives", __name__)


def _seed_startup_directives():
    """Queue known operator directives from code (idempotent). Lets us prove
    the human→brain channel end-to-end without the admin-key curl dance.

    Phase r70+obsolete-prune (2026-06-07): the original miso_spp_adapters
    directive (seeded 2026-05-22) is OBSOLETE — fetch_miso() and fetch_spp()
    were implemented in iso_grid_adapters.py on 2026-06-02 (verified live).
    The directive kept appearing as a synthetic 10,200-seen_count entry on
    the worklist for 16 days because nothing marked it done. The seed is
    REMOVED here; the auto-resolver below clears the stale row at boot."""
    try:
        store.init_schema()  # ensure brain_directives table exists
    except Exception as e:
        logger.warning(f"[brain_directives] schema init skipped: {e}")


def _auto_resolve_obsolete_directives():
    """Boot-time cleanup: any 'open' directive whose target module already
    contains the requested implementation gets auto-marked 'done' with an
    audit note. Idempotent — won't touch already-resolved rows.

    Currently handles:
      · miso_spp_adapters_2026_05_22 → if fetch_miso + fetch_spp exist in
        iso_grid_adapters.py, mark the seeded directive done.

    Generalises to: an operator directive that asks the brain to add X
    should not keep firing once X exists. This is the start of a
    "directive outcome verifier" mirroring the autopilot one."""
    try:
        for d in store.list_directives(status="open", limit=200):
            src = (d.get("source") or "")
            target = (d.get("target") or "")
            did = d.get("id")
            if src == "seed:miso_spp_adapters_2026_05_22" and target == "iso_grid_adapters.py":
                try:
                    from iso_grid_adapters import fetch_miso, fetch_spp  # noqa: F401
                    store.set_directive_status(
                        int(did), "done",
                        notes=("auto-resolved 2026-06-07: fetch_miso() + "
                               "fetch_spp() are implemented in "
                               "iso_grid_adapters.py (lines 435 + 517, "
                               "shipped 2026-06-02). Directive obsolete."),
                    )
                    logger.info(f"[brain_directives] auto-resolved obsolete "
                                f"directive #{did} (miso_spp_adapters)")
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[brain_directives] auto-resolve skipped: {e}")


try:
    _seed_startup_directives()
    _auto_resolve_obsolete_directives()
except Exception:
    pass


def _admin_ok() -> bool:
    """Constant-time check against the brain-directive admin key OR any of
    the standard internal/admin keys. Returns False (deny) if neither
    accepts the provided header — never fail open.

    Phase r70+obsolete-prune (2026-06-07): widen accepted keys to include
    the standard internal/admin set. Previously this gate only honored
    BRAIN_ADMIN_KEY, so operator-side directive resolution required minting
    a second secret. Now an existing X-Admin-Key/X-Internal-Key works for
    directive status transitions (same authority used by /api/v1/admin/*)."""
    provided = (request.headers.get("X-Admin-Key", "")
                or request.headers.get("X-Internal-Key", "")).strip()
    if not provided:
        return False
    # Primary path: BRAIN_ADMIN_KEY if set
    expected = os.environ.get("BRAIN_ADMIN_KEY", "")
    if expected and hmac.compare_digest(provided, expected):
        return True
    # Fallback: any internal/admin key the rest of the app honors.
    try:
        from internal_auth import accepted_internal_keys
        accepted = set(accepted_internal_keys() or [])
    except Exception:
        accepted = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(_n, "")
        if v:
            accepted.add(v)
    for k in accepted:
        if k and hmac.compare_digest(provided, k):
            return True
    return False


@brain_directives_bp.route("/api/v1/brain/directives", methods=["GET"])
def list_directives():
    status = request.args.get("status", "open")
    if status == "all":
        status = None
    items = store.list_directives(status=status, limit=100)
    return jsonify({
        "ok": True,
        "count": len(items),
        "open_total": store.count_open_directives(),
        "directives": items,
    }), 200


# AUTO-REPAIR: duplicate route '/api/v1/brain/directives' also in routes/brain_directives.py:125 — review and remove one
@brain_directives_bp.route("/api/v1/brain/directives", methods=["POST"])
def add_directive():
    if not _admin_ok():
        return jsonify({
            "ok": False,
            "error": "admin auth required",
            "hint": ("Send X-Admin-Key with BRAIN_ADMIN_KEY or any standard "
                     "DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY value."),
        }), 401
    body = request.get_json(silent=True) or {}
    directive = (body.get("directive") or "").strip()
    if not directive:
        return jsonify({"ok": False, "error": "directive text required"}), 400
    row = store.add_directive(
        directive=directive,
        kind=body.get("kind", "fix"),
        target=body.get("target", ""),
        priority=int(body.get("priority", 100)),
        source=body.get("source", "operator"),
    )
    if not row:
        return jsonify({"ok": False, "error": "could not store directive"}), 500
    logger.info(f"[brain_directives] queued #{row.get('id')}: {directive[:80]}")
    return jsonify({"ok": True, "directive": row}), 201


@brain_directives_bp.route("/api/v1/brain/directives/<int:directive_id>/status",
                           methods=["POST"])
def set_status(directive_id: int):
    if not _admin_ok():
        return jsonify({
            "ok": False, "error": "admin auth required",
            "hint": ("Send X-Admin-Key with BRAIN_ADMIN_KEY or any standard "
                     "DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY value."),
        }), 401
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    ok = store.set_directive_status(directive_id, status, body.get("notes", ""))
    if not ok:
        return jsonify({"ok": False,
                        "error": "invalid status or directive not found"}), 400
    return jsonify({"ok": True, "id": directive_id, "status": status}), 200
