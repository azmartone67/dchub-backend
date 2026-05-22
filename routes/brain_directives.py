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
    the human→brain channel end-to-end without the admin-key curl dance."""
    try:
        store.init_schema()  # ensure brain_directives table exists
        store.seed_directive_once(
            marker="miso_spp_adapters_2026_05_22",
            directive=("Find current public real-time generation + load "
                       "endpoints for MISO and SPP (old paths returned "
                       "404/empty on 2026-05-22), then implement fetch_miso() "
                       "and fetch_spp() in iso_grid_adapters.py matching the "
                       "NYISO/CAISO adapter pattern."),
            kind="create", target="iso_grid_adapters.py", priority=200,
        )
    except Exception as e:
        logger.warning(f"[brain_directives] seed skipped: {e}")


try:
    _seed_startup_directives()
except Exception:
    pass


def _admin_ok() -> bool:
    """Constant-time check of X-Admin-Key against BRAIN_ADMIN_KEY env.
    Returns False (deny) if the env var is unset — never fail open."""
    expected = os.environ.get("BRAIN_ADMIN_KEY", "")
    if not expected:
        return False
    provided = request.headers.get("X-Admin-Key", "")
    return bool(provided) and hmac.compare_digest(provided, expected)


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


@brain_directives_bp.route("/api/v1/brain/directives", methods=["POST"])
def add_directive():
    if not _admin_ok():
        return jsonify({
            "ok": False,
            "error": "admin auth required",
            "hint": "Set BRAIN_ADMIN_KEY on the backend and send X-Admin-Key.",
        }), (503 if not os.environ.get("BRAIN_ADMIN_KEY") else 401)
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
        }), (503 if not os.environ.get("BRAIN_ADMIN_KEY") else 401)
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    ok = store.set_directive_status(directive_id, status, body.get("notes", ""))
    if not ok:
        return jsonify({"ok": False,
                        "error": "invalid status or directive not found"}), 400
    return jsonify({"ok": True, "id": directive_id, "status": status}), 200
