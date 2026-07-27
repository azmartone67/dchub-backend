"""routes/downloads.py — installable artifacts (digest wave 2, 2026-07-27).

GET /downloads/dchub.dxt — the one-click Claude Desktop extension (a
dependency-free stdio⇄HTTP bridge to the hosted /mcp server; source in the
dchub-mcp-server repo under dxt/). Claude is the single largest external
platform (115,963 requests/7d) and today every one of those installs is a
copy-paste JSON config; dxt.so lists us with no installable artifact. This
route is the artifact.

Static file, long cache (immutable content is versioned by filename if it
ever needs to be), correct MIME so browsers download instead of render.
"""

from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, send_from_directory

logger = logging.getLogger(__name__)

downloads_bp = Blueprint("downloads", __name__)

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "static", "downloads")


@downloads_bp.route("/downloads/dchub.dxt", methods=["GET"])
def dchub_dxt():
    try:
        resp = send_from_directory(
            _DIR, "dchub.dxt", mimetype="application/octet-stream",
            as_attachment=True, download_name="dchub.dxt")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception as e:  # noqa: BLE001
        logger.warning("dxt download failed: %s", e)
        return jsonify(ok=False, error="artifact unavailable"), 404
