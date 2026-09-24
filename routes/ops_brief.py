"""routes/ops_brief.py — the PUBLIC, keyless agent brief.

WHY THIS EXISTS (2026-09-24)
----------------------------
Outside agents (Grok first) propose changes to DC Hub from what they can see.
They cannot see what we have already measured, refuted, frozen or decided. On
2026-09-24 that produced three misses in one day:
  • a live registry paste the repo's auto-writer would have reverted,
  • a timeout raise for a probe already at 25s,
  • a ranking theory we had refuted on 2026-09-03.
Grok agreed to read this brief at the start of every run and cite its ids.

It is a sibling of /api/v1/ops/deadman and /api/v1/ops/claims: keyless,
under the same edge-bypassed /api/v1/ops/ prefix. The content is the
committed file data/agent_brief.json, so it changes only by PR. `as_of` is
when it was last reviewed; `served_at` is when this response was made.

Kill switch OPS_BRIEF_DISABLE=1 answers 404, never 5xx. An unreadable file
answers 503 with ok:false, never an empty brief that reads as "nothing known".
"""
import json
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify

ops_brief_bp = Blueprint("ops_brief", __name__)

_BRIEF_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "agent_brief.json")
_EDGE_TTL_S = 300


def _load_brief(path: str = _BRIEF_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@ops_brief_bp.route("/api/v1/ops/brief", methods=["GET"])
def ops_brief():
    """PUBLIC, keyless. Read `purpose` first."""
    served_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if os.environ.get("OPS_BRIEF_DISABLE", "").strip() in ("1", "true", "yes"):
        resp = jsonify(ok=False, error="disabled", note="OPS_BRIEF_DISABLE=1")
        resp.headers["Cache-Control"] = "no-store"
        return resp, 404
    try:
        brief = _load_brief()
    except Exception as e:  # missing or malformed file: say so, never serve an empty brief
        resp = jsonify(ok=False, error="brief_unavailable",
                       detail=f"{type(e).__name__}: {str(e)[:160]}", served_at=served_at)
        resp.headers["Cache-Control"] = "no-store"
        return resp, 503
    body = {"ok": True, "served_at": served_at, **brief}
    resp = jsonify(body)
    resp.headers["Cache-Control"] = f"public, max-age=0, s-maxage={_EDGE_TTL_S}, must-revalidate"
    resp.headers["CDN-Cache-Control"] = f"max-age={_EDGE_TTL_S}"
    return resp


def register_ops_brief(app) -> bool:
    app.register_blueprint(ops_brief_bp)
    return True
