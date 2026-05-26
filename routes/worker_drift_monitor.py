"""
worker_drift_monitor.py — surface the dchubapiproxy worker version drift.

Phase ZZZZZ-round47.19 (2026-05-26). Three MCP manifest surfaces, three
versions:
  POST /mcp initialize           2.1.10  (live server, correct)
  /api/v1/mcp/manifest (Flask)   2.1.10  (r47.12 dynamic mirror, fixed)
  /mcp/manifest (zone worker)    2.1.5   (stuck — dchubapiproxy needs CF
                                          dashboard update)

This blueprint polls all three surfaces + reports the gap so the user
can see at-a-glance "yes, still stuck" or "fixed!" without manually
hitting three URLs.

Endpoint:
  GET /api/v1/admin/drift-check
    → {
        "drift_detected": true|false,
        "expected_version": "2.1.10",
        "surfaces": [...],
        "action_required": "...",
      }
"""
import os
import json
import datetime
import urllib.request
from flask import Blueprint, jsonify

worker_drift_bp = Blueprint("worker_drift_monitor", __name__)


_SURFACES = [
    {
        "name":  "Live MCP server",
        "method": "POST_INIT",
        "url":   "https://dchub.cloud/mcp",
        "owner": "dchub-mcp-server (Node, Railway)",
        "source_of_truth": True,
    },
    {
        "name":  "Flask mirror manifest",
        "method": "GET",
        "url":   "https://api.dchub.cloud/api/v1/mcp/manifest",
        "owner": "Flask backend, this repo (r47.12 dynamic fetch)",
    },
    {
        "name":  "Zone worker manifest",
        "method": "GET",
        "url":   "https://dchub.cloud/mcp/manifest",
        "owner": "dchubapiproxy worker (out-of-repo, CF dashboard)",
    },
]


def _fetch_version(surface):
    """Probe a surface, return its reported version + any error."""
    try:
        if surface["method"] == "GET":
            req = urllib.request.Request(
                surface["url"],
                headers={"User-Agent": "DCHub-DriftMonitor/1.0",
                          "X-DC-Internal-Warmup": "1"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
                return {
                    "version":     d.get("version"),
                    "tools_count": len(d.get("tools") or []),
                    "tools_source": d.get("tools_source", "-"),
                    "ok": True,
                }
        elif surface["method"] == "POST_INIT":
            payload = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "drift-monitor", "version": "1"}},
            }).encode("utf-8")
            req = urllib.request.Request(
                surface["url"], data=payload,
                headers={"Content-Type": "application/json",
                          "Accept": "application/json, text/event-stream",
                          "User-Agent": "DCHub-DriftMonitor/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read().decode("utf-8", errors="ignore")
                for line in body.split("\n"):
                    if line.startswith("data:"):
                        try:
                            d = json.loads(line[5:].strip())
                            v = (d.get("result") or {}).get("serverInfo", {}).get("version")
                            if v: return {"version": v, "tools_count": None, "ok": True}
                        except Exception:
                            pass
                return {"version": None, "ok": False, "error": "no_sse_data"}
    except Exception as e:
        return {"version": None, "ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


@worker_drift_bp.route("/api/v1/admin/drift-check", methods=["GET"], strict_slashes=False)
def drift_check():
    results = []
    expected = None
    for s in _SURFACES:
        probe = _fetch_version(s)
        results.append({**s, **probe})
        if s.get("source_of_truth"):
            expected = probe.get("version")

    # Compute drift
    drift_detected = False
    drift_items = []
    for r in results:
        if r.get("source_of_truth"): continue
        if r.get("version") and expected and r["version"] != expected:
            drift_detected = True
            drift_items.append({
                "surface": r["name"],
                "owner":   r["owner"],
                "reports": r["version"],
                "expected": expected,
            })

    action_required = None
    if drift_detected:
        actions = []
        for d in drift_items:
            if "dchubapiproxy" in (d.get("owner") or ""):
                actions.append(
                    "CF Dashboard → Workers & Pages → dchubapiproxy → edit "
                    "source → bump version field to '{}' (or add /mcp/manifest "
                    "to the proxied paths list so it falls through to Flask)."
                    .format(expected or "current")
                )
            else:
                actions.append(f"Investigate {d['surface']} (owner: {d['owner']})")
        action_required = " · ".join(actions)

    return jsonify({
        "drift_detected":  drift_detected,
        "expected_version": expected,
        "surfaces":        results,
        "drift_items":     drift_items,
        "action_required": action_required or "No drift — all surfaces match.",
        "checked_at":      datetime.datetime.utcnow().isoformat() + "Z",
    }), 200, {"Cache-Control": "public, max-age=120"}
