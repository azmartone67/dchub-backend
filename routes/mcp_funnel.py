"""Phase GGG (2026-05-16) — per-tool MCP conversion funnel.

Today we know `get_grid_intelligence` had 3,961 calls / 0 conversions
in 14 days. We DON'T know:
  - How many of those minted a pair-code (paywall → code stage)
  - How many users hit /redeem/<code>           (code → view stage)
  - How many clicked the Stripe link            (view → click stage)
  - How many completed payment                  (click → pay stage)

Without this, we can't tell WHERE the funnel leaks. This module joins
mcp_call_log → mcp_upgrade_signals → mcp_pair_codes → redeemed_at
and exposes per-stage drop rates per tool. Reveals the exact stage
where the conversion engine is broken.

  GET /api/v1/mcp/conversion-funnel              — all paid tools
  GET /api/v1/mcp/conversion-funnel/<tool>       — single-tool drilldown

Brain detector check_mcp_funnel_leak fires when any stage drops >95%
on a tool with >50 entries — that's the bottleneck the autopilot
escalates so humans see exactly what to fix.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


mcp_funnel_bp = Blueprint("mcp_funnel_v2", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _compute_funnel(tool_filter: str | None = None, days: int = 14) -> list[dict]:
    """Returns one dict per tool with all funnel stages."""
    c = _conn()
    if c is None: return []
    out: list[dict] = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Stage 0: total calls per tool
            tools_query = """
                SELECT tool, COUNT(*) AS total_calls,
                       COUNT(DISTINCT api_key)  AS unique_keys,
                       COUNT(*) FILTER (WHERE status IN ('ok','success','200')) AS ok_calls
                  FROM mcp_call_log
                 WHERE timestamp >= NOW() - INTERVAL '%s days'
                   AND tool IS NOT NULL
            """ % days
            if tool_filter:
                tools_query += " AND tool = %s GROUP BY tool"
                cur.execute(tools_query, (tool_filter,))
            else:
                tools_query += " GROUP BY tool HAVING COUNT(*) >= 10 ORDER BY total_calls DESC LIMIT 25"
                cur.execute(tools_query)
            tools = cur.fetchall()

            for t in tools:
                tool = t["tool"]
                entry = {
                    "tool":           tool,
                    "window_days":    days,
                    "stages": {
                        "0_total_calls":      int(t["total_calls"]),
                        "0_unique_keys":      int(t["unique_keys"] or 0),
                        "0_ok_calls":         int(t["ok_calls"] or 0),
                        "1_paywall_signals":  0,
                        "2_codes_minted":     0,
                        "3_redeem_viewed":    0,
                        "4_stripe_clicked":   0,
                        "5_converted":        0,
                    },
                    "drop_rates":      {},
                    "biggest_leak":    None,
                }

                # Stage 1: paywall signals. Phase UUU (2026-05-16):
                # the actual schema column is `tool_requested` (set by
                # main.py:21632 + routes/pair_code.py:466). Earlier the
                # funnel probed `tool` then `tool_name`, neither of which
                # exist on the table — so stage-1 was always zero and we
                # showed 100% drop at 0→1. Now we try `tool_requested`
                # first; `tool`/`tool_name` kept as defensive fallbacks
                # for older deploys.
                got_signals = False
                for col in ("tool_requested", "tool", "tool_name"):
                    try:
                        cur.execute(f"""
                            SELECT COUNT(*) FROM mcp_upgrade_signals
                             WHERE {col} = %s
                               AND created_at >= NOW() - INTERVAL '%s days'
                        """, (tool, days))
                        n = int((cur.fetchone() or [0])[0] or 0)
                        if n > 0:
                            entry["stages"]["1_paywall_signals"] = n
                            got_signals = True
                            break
                    except Exception:
                        continue
                # Derived fallback: non-ok call-log entries = signals proxy
                if not got_signals:
                    try:
                        cur.execute("""
                            SELECT COUNT(*) FROM mcp_call_log
                             WHERE tool = %s
                               AND timestamp >= NOW() - INTERVAL '%s days'
                               AND status NOT IN ('ok','success','200')
                        """, (tool, days))
                        entry["stages"]["1_paywall_signals"] = int((cur.fetchone() or [0])[0] or 0)
                        entry["_signals_source"] = "derived_from_non_ok_call_log"
                    except Exception:
                        pass

                # Stages 2-5 from mcp_pair_codes (tool_name column)
                try:
                    cur.execute("""
                        SELECT
                          COUNT(*) AS minted,
                          COUNT(*) FILTER (WHERE redeem_viewed_at IS NOT NULL)  AS viewed,
                          COUNT(*) FILTER (WHERE stripe_clicked_at IS NOT NULL) AS clicked,
                          COUNT(*) FILTER (WHERE redeemed_at IS NOT NULL)       AS converted
                          FROM mcp_pair_codes
                         WHERE tool_name = %s
                           AND created_at >= NOW() - INTERVAL '%s days'
                    """, (tool, days))
                    r = cur.fetchone() or {}
                    entry["stages"]["2_codes_minted"]   = int(r.get("minted") or 0)
                    entry["stages"]["3_redeem_viewed"]  = int(r.get("viewed") or 0)
                    entry["stages"]["4_stripe_clicked"] = int(r.get("clicked") or 0)
                    entry["stages"]["5_converted"]      = int(r.get("converted") or 0)
                except Exception:
                    pass

                # Drop rates: pct of stage_N that did NOT make it to stage_N+1
                s = entry["stages"]
                def _drop(a, b):
                    if a == 0: return None
                    return round(100.0 * (1 - (b / a)), 1)
                entry["drop_rates"] = {
                    "0_call_to_1_signal":          _drop(s["0_total_calls"],     s["1_paywall_signals"]),
                    "1_signal_to_2_code":          _drop(s["1_paywall_signals"], s["2_codes_minted"]),
                    "2_code_to_3_viewed":          _drop(s["2_codes_minted"],    s["3_redeem_viewed"]),
                    "3_viewed_to_4_clicked":       _drop(s["3_redeem_viewed"],   s["4_stripe_clicked"]),
                    "4_clicked_to_5_converted":    _drop(s["4_stripe_clicked"],  s["5_converted"]),
                }

                # Identify the biggest leak
                best_stage = None
                best_drop  = -1
                for stage, drop in entry["drop_rates"].items():
                    if drop is None: continue
                    if drop > best_drop:
                        best_drop = drop
                        best_stage = stage
                if best_stage:
                    entry["biggest_leak"] = {
                        "stage":         best_stage,
                        "drop_pct":      best_drop,
                        "starting_volume": s.get(best_stage.split("_to_")[0] + "_total_calls"  if best_stage.startswith("0_") else
                                                  best_stage.split("_to_")[0] + "_paywall_signals" if best_stage.startswith("1_") else
                                                  best_stage.split("_to_")[0] + "_codes_minted"  if best_stage.startswith("2_") else
                                                  best_stage.split("_to_")[0] + "_redeem_viewed" if best_stage.startswith("3_") else
                                                  best_stage.split("_to_")[0] + "_stripe_clicked"),
                    }
                out.append(entry)
    finally:
        try: c.close()
        except Exception: pass
    return out


# AUTO-REPAIR: duplicate route '/api/v1/mcp/conversion-funnel' also in main.py:22613 — review and remove one
@mcp_funnel_bp.route("/api/v1/mcp/conversion-funnel", methods=["GET"])
def conversion_funnel():
    """All paid + commonly-called tools with per-stage funnel."""
    try: days = max(1, min(90, int(request.args.get("days") or 14)))
    except ValueError: days = 14
    funnels = _compute_funnel(tool_filter=None, days=days)
    resp = jsonify(
        funnels=funnels,
        window_days=days,
        total_tools=len(funnels),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        legend={
            "0_total_calls":      "mcp_call_log row count (any status)",
            "1_paywall_signals":  "mcp_upgrade_signals row count (paywall fired)",
            "2_codes_minted":     "mcp_pair_codes created (agent got a /redeem URL)",
            "3_redeem_viewed":    "redeem_viewed_at is not null (human landed on /redeem/<code>)",
            "4_stripe_clicked":   "stripe_clicked_at is not null (human clicked Upgrade)",
            "5_converted":        "redeemed_at is not null (Stripe webhook fired)",
        },
    )
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@mcp_funnel_bp.route("/api/v1/mcp/conversion-funnel/<tool>", methods=["GET"])
def conversion_funnel_tool(tool):
    try: days = max(1, min(90, int(request.args.get("days") or 14)))
    except ValueError: days = 14
    funnels = _compute_funnel(tool_filter=tool, days=days)
    if not funnels: return jsonify(error="no_data", tool=tool), 404
    return jsonify(funnels[0]), 200
