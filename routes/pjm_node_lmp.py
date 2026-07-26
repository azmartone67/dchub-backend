"""Shell #35 WS8b (2026-07-26) — PJM nodal LMP on the protected gridstatus
budget (owner: "don't burn gridstatus on anything but PJM").

  GET /api/v1/pjm/node-lmp?node=PSEG&market=da|rt&hours=24

Premium depth nobody else serves agent-native: per-node PJM LMP (DA hourly
or RT 5-min) via gridstatus (licensed redistributor). Quota discipline:
  * identified+ only (X-API-Key required) — anon can't burn quota
  * 12h in-process cache per (node, market)
  * every upstream call goes through pjm_dataminer._gridstatus_get →
    counted by the monthly ledger (GRIDSTATUS_MONTHLY_BUDGET hard cap)
  * small row limits (DA 48 rows, RT 24 rows) — rows count against the
    500k/mo row-export quota too
"""

from __future__ import annotations

import os
import re
import time
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

pjm_node_lmp_bp = Blueprint("pjm_node_lmp", __name__)

_NODE_RE = re.compile(r"^[A-Za-z0-9_\-\.]{2,40}$")
_CACHE: dict = {}
_TTL_S = int(os.environ.get("PJM_NODE_LMP_CACHE_TTL_S", "43200"))  # 12h

_MARKETS = {"da": ("pjm_lmp_day_ahead_hourly", 48),
            "rt": ("pjm_lmp_real_time_5_min", 24)}


def _fetch_node(node: str, market: str) -> dict:
    from pjm_dataminer import _gridstatus_get
    dataset, limit = _MARKETS[market]
    rows, err = _gridstatus_get(dataset, {
        "filter_column": "location", "filter_value": node,
        "limit": limit, "order": "desc"})
    if err:
        return {"error": err}
    out_rows = []
    for r in rows or []:
        try:
            out_rows.append({
                "period": r.get("interval_start_utc"),
                "lmp": float(r["lmp"]) if r.get("lmp") is not None else None,
                "congestion": (float(r["congestion"])
                               if r.get("congestion") is not None else None),
                "loss": float(r["loss"]) if r.get("loss") is not None else None,
            })
        except (TypeError, ValueError, KeyError):
            continue
    if not out_rows:
        return {"error": "no_rows_for_node"}
    lmps = [r["lmp"] for r in out_rows if r["lmp"] is not None]
    return {
        "node": node, "market": market, "dataset": dataset,
        "rows": out_rows,
        "stats": {"latest": out_rows[0]["lmp"],
                  "avg": round(sum(lmps) / len(lmps), 2) if lmps else None,
                  "max": max(lmps) if lmps else None,
                  "min": min(lmps) if lmps else None,
                  "n": len(out_rows)},
        "as_of": out_rows[0]["period"],
        "source": "gridstatus.io (licensed PJM redistribution)",
        "cache_ttl_hours": _TTL_S / 3600,
        "note": ("Nodal PJM LMP, budget-guarded (gridstatus free tier). "
                 "Cached %dh per node+market." % (_TTL_S // 3600)),
    }


@pjm_node_lmp_bp.route("/api/v1/pjm/node-lmp", methods=["GET"])
def node_lmp():
    api_key = (request.headers.get("X-API-Key")
               or request.args.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="identified_required",
                       message=("PJM nodal LMP is identified+ (any key — "
                                "free keys work). Claim one via the MCP "
                                "claim_free_key tool or /pricing."),
                       upgrade_url="https://dchub.cloud/pricing"), 402
    node = (request.args.get("node") or "").strip().upper()
    market = (request.args.get("market") or "da").strip().lower()
    if not _NODE_RE.match(node):
        return jsonify(error="bad_node",
                       hint="node = PJM pnode name, e.g. PSEG, DOM, WESTERN HUB"
                       ), 400
    if market not in _MARKETS:
        return jsonify(error="bad_market", allowed=sorted(_MARKETS)), 400
    ck = f"{node}|{market}"
    hit = _CACHE.get(ck)
    now = time.time()
    if hit and now - hit[0] < _TTL_S:
        body = dict(hit[1])
        body["cached"] = True
        return jsonify(body), 200
    try:
        body = _fetch_node(node, market)
    except Exception as e:
        logger.warning("pjm_node_lmp: %s", str(e)[:120])
        body = {"error": f"{type(e).__name__}"}
    if body.get("error"):
        # negative-cache 10 min so a bad node can't drain the budget
        _CACHE[ck] = (now - _TTL_S + 600, body)
        code = 502 if "budget" in str(body.get("error")) else 404
        return jsonify(body), code
    _CACHE[ck] = (now, body)
    body = dict(body)
    body["cached"] = False
    return jsonify(body), 200
