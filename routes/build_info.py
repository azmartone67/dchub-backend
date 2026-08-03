"""build_info.py — "is my merge actually running?" in one call (2026-08-03).

WHY THIS EXISTS
===============
Shell #50 merged, `/api/v1/admin/revenue/master-tick` returned 404, and there
was no way to tell which of these it was:

  · the deploy has not landed yet (the build predates the merge), or
  · the blueprint raised at import and main.py's try/except swallowed it into
    a logger warning nobody reads, so the route will 404 FOREVER.

Both look identical from outside: a bare 404. The only reason we resolved it
was noticing that the 404 handler's `suggestions` list is built from the LIVE
url_map, so its contents implied the route was absent from the running build.
That is detective work, not diagnostics, and it will be needed again on the
next merge.

★THE COMMIT SHA IS THE ANSWER. Railway exports RAILWAY_GIT_COMMIT_SHA for the
running deploy. Comparing it to the SHA you just merged answers "is it live?"
with no inference at all. Everything else in this module is secondary.

★AND `?route=` ANSWERS THE OTHER HALF. Asking the live url_map whether a path
resolves distinguishes "not deployed" (SHA is old) from "deployed but failed to
register" (SHA is current AND the route is missing) — which is the case that
means a real bug rather than patience.

Endpoint:
  GET /api/v1/admin/build-info
  GET /api/v1/admin/build-info?route=/api/v1/admin/revenue/master-tick

Auth: X-Admin-Key / ?admin_key=. Read-only; touches no database.
Kill: BUILD_INFO_DISABLE=1
"""
from __future__ import annotations

import logging
import os
import time

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)
build_info_bp = Blueprint("build_info", __name__)

# Stamped at import — the closest thing to "when did this process start".
_BOOT_MONOTONIC = time.time()

# Railway's own deploy metadata. Named in priority order; the first that is set
# wins. ★No fallback to a git command: this runs inside a container that may not
# have a .git directory, and a shelled-out SHA would report the BUILD image's
# commit rather than the running deploy's — a confidently wrong answer to the
# one question the module exists to answer.
_SHA_VARS = ("RAILWAY_GIT_COMMIT_SHA", "RAILWAY_DEPLOYMENT_ID",
             "SOURCE_COMMIT", "GIT_COMMIT", "HEROKU_SLUG_COMMIT")


def _disabled() -> bool:
    return (os.environ.get("BUILD_INFO_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def running_commit() -> dict:
    """{var, sha} for the running deploy, or {var: None} when the platform did
    not tell us. ★None is NOT "unknown-but-probably-latest" — a caller must
    treat it as unanswerable rather than assume freshness."""
    for var in _SHA_VARS:
        val = (os.environ.get(var) or "").strip()
        if val:
            return {"var": var, "sha": val, "short": val[:8]}
    return {"var": None, "sha": None, "short": None,
            "note": ("no deploy-SHA env var set (" + ", ".join(_SHA_VARS) +
                     ") — the running commit is UNANSWERABLE from inside the "
                     "container, not 'current'.")}


def route_exists(path: str) -> dict:
    """Does `path` resolve in the LIVE url_map? Exact-match first, then a
    rule-string comparison so a parameterised rule still answers honestly."""
    out = {"route": path, "exists": None}
    try:
        rules = list(current_app.url_map.iter_rules())
    except Exception as e:  # noqa: BLE001
        out["error"] = f"url_map unreadable: {str(e)[:120]}"
        return out
    want = (path or "").strip()
    if not want:
        out["error"] = "empty route"
        return out
    exact = [r for r in rules if str(r) == want]
    if exact:
        out["exists"] = True
        out["endpoint"] = exact[0].endpoint
        out["methods"] = sorted((exact[0].methods or set()) - {"HEAD", "OPTIONS"})
        return out
    # A miss is a real answer, and the neighbours make it actionable: a route
    # whose siblings are present is a registration failure; one whose whole
    # prefix is missing is usually a deploy that has not landed.
    prefix = want.rsplit("/", 1)[0]
    out["exists"] = False
    out["siblings"] = sorted({str(r) for r in rules
                              if str(r).startswith(prefix)})[:12]
    out["interpretation"] = (
        "siblings present but this route missing → the blueprint likely FAILED "
        "to register (check Railway logs for 'wiring failed'); no siblings "
        "either → the deploy carrying it has probably not landed yet. Compare "
        "`commit` against the SHA you merged to settle it."
        if out["siblings"] else
        "no sibling routes under this prefix — the deploy carrying it has "
        "probably not landed. Compare `commit` against the SHA you merged.")
    return out


@build_info_bp.route("/api/v1/admin/build-info", methods=["GET"])
def build_info():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    out = {"ok": True, "commit": running_commit(),
           "uptime_s": int(time.time() - _BOOT_MONOTONIC)}
    try:
        out["routes_total"] = len(list(current_app.url_map.iter_rules()))
    except Exception as e:  # noqa: BLE001
        out["routes_total"] = None
        out["routes_error"] = str(e)[:120]
    want = (request.args.get("route") or "").strip()
    if want:
        out["route_check"] = route_exists(want)
    out["how_to_read"] = (
        "commit.sha is the RUNNING deploy. If it differs from the SHA you "
        "merged, the deploy has not landed and a 404 is expected — wait. If it "
        "MATCHES and ?route= says exists=false, the blueprint failed to "
        "register and the 404 is permanent: that is a bug, not patience.")
    return jsonify(out)
