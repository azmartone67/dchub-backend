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

# 2026-06-15: the stage-0 demand numbers (total_calls / unique_keys per tool)
# read mcp_call_log, which — unlike mcp_upgrade_signals (filtered at write time
# by _is_synthetic) — logs OUR OWN monitoring + test traffic. dchub-selfheal
# alone is ~70% of all call volume (96,974 of 138,860 rows/30d), so "193 distinct
# users on get_grid_intelligence" is mostly internal probes, not addressable
# demand. Exclude synthetic clients here too, reusing the canonical prefix list
# (single source of truth) so the demand pool reflects REAL external agents.
try:
    from mcp_upgrade_gate import _SYNTHETIC_CLIENT_PREFIXES as _SYNTH_PREFIXES
except Exception:
    _SYNTH_PREFIXES = ('dchub-', 'step2_', 'qa-', 'probe-', 'test-', 'monitor-',
                       'healthcheck', 'r51-', 'r52-', 'e2e-', 'recheck')
# mcp_call_log.platform carries the client label. Build a NOT-LIKE clause +
# params from the prefix list; empty list → no-op clause.
_SYNTH_CALL_CLAUSE = "".join(
    " AND LOWER(COALESCE(platform,'')) NOT LIKE %s" for _ in _SYNTH_PREFIXES
)
_SYNTH_CALL_PARAMS = tuple(str(p).lower() + "%" for p in _SYNTH_PREFIXES)


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
                 WHERE timestamp >= NOW() - %s * INTERVAL '1 day'
                   AND tool IS NOT NULL
            """ + _SYNTH_CALL_CLAUSE   # exclude our own monitoring/test traffic
            # r88 (2026-06-15): days is now a BOUND psycopg2 param everywhere (the
            # `%s * INTERVAL '1 day'` + Python `% days` mix was the recurring
            # "tuple index out of range" trap the brain kept flagging — a literal %
            # anywhere in the string would make `% days` throw, and a params/
            # placeholder misalign would crash the whole funnel signal).
            # Param order MUST be: days (timestamp) → synthetic NOT-LIKE params →
            # optional tool_filter, matching the clause order in the SQL.
            if tool_filter:
                tools_query += " AND tool = %s GROUP BY tool"
                cur.execute(tools_query, (days, *_SYNTH_CALL_PARAMS, tool_filter))
            else:
                tools_query += " GROUP BY tool HAVING COUNT(*) >= 10 ORDER BY total_calls DESC LIMIT 25"
                cur.execute(tools_query, (days, *_SYNTH_CALL_PARAMS))
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
                            SELECT COUNT(*) AS n FROM mcp_upgrade_signals
                             WHERE {col} = %s
                               AND created_at >= NOW() - %s * INTERVAL '1 day'
                        """, (tool, days))
                        # RealDictCursor (line 52) returns dicts — the prior
                        # cur.fetchone()[0] raised KeyError (swallowed by the bare
                        # except), zeroing stage-1 for EVERY tool while signals
                        # were written fine. Access by alias key.
                        n = int((cur.fetchone() or {}).get("n") or 0)
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
                            SELECT COUNT(*) AS n FROM mcp_call_log
                             WHERE tool = %s
                               AND timestamp >= NOW() - %s * INTERVAL '1 day'
                               AND status NOT IN ('ok','success','200')
                        """, (tool, days))
                        entry["stages"]["1_paywall_signals"] = int((cur.fetchone() or {}).get("n") or 0)
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
                           AND created_at >= NOW() - %s * INTERVAL '1 day'
                    """, (tool, days))
                    r = cur.fetchone() or {}
                    entry["stages"]["2_codes_minted"]   = int(r.get("minted") or 0)
                    entry["stages"]["3_redeem_viewed"]  = int(r.get("viewed") or 0)
                    entry["stages"]["4_stripe_clicked"] = int(r.get("clicked") or 0)
                    entry["stages"]["5_converted"]      = int(r.get("converted") or 0)
                except Exception:
                    pass

                # r85i: HONEST per-tool conversion. Stages 2-4 above come from
                # mcp_pair_codes — a DIFFERENT, smaller flow than the per-tool
                # paywall signals (stage 1, mcp_upgrade_signals). A paywall hit on
                # tool X rarely produces a pair-code row with tool_name=X, so
                # stages 2-5 read ~0 and the funnel screamed a fake "100% leak"
                # per tool forever (the recurring mcp_funnel_leak finding). The
                # REAL per-tool conversion is the signal's OWN `converted` flag,
                # counted by DISTINCT caller (raw signals are loop-inflated; a
                # converter who hit the paywall 6x must not count 6x). Override
                # 5_converted with that + record distinct_callers; compute the
                # leak on the honest signal->converted path, not the pair-code chain.
                # 2026-06-15: caller_id is now populated on every new signal (the
                # legacy fire_upgrade_signal NULL-caller_id regression is fixed) and
                # the historical NULL rows were backfilled, so we can finally dedupe
                # converters by caller_id. COUNT(*) FILTER (WHERE converted) was
                # inflating the conversion stage: mark_signals_converted() flips
                # EVERY signal for a converting email, so one buyer who hit the
                # paywall N times counted as N conversions (e.g. 69 "conversions"
                # were all one operator email flipped at a single instant). Count
                # DISTINCT converters instead.
                distinct_callers, converted_signals = 0, 0
                try:
                    cur.execute("""
                        SELECT COUNT(DISTINCT caller_id)                          AS callers,
                               COUNT(DISTINCT caller_id) FILTER (WHERE converted)  AS converted
                          FROM mcp_upgrade_signals
                         WHERE tool_requested = %s
                           AND created_at >= NOW() - %s * INTERVAL '1 day'
                    """, (tool, days))
                    rr = cur.fetchone() or {}
                    distinct_callers  = int(rr.get("callers") or 0)
                    converted_signals = int(rr.get("converted") or 0)
                except Exception:
                    pass
                # 2026-06-28: the signal `converted` flag is dead (nothing ever
                # set it — mark_signals_converted() never existed), so per-tool
                # conversion read 0 for EVERY tool. The real per-tool signal is
                # conversion_attribution (tool parsed from the Stripe
                # client_reference_id at checkout). Prefer it; fall back to the
                # signal flag if attribution has no row yet.
                attr_conv = 0
                try:
                    from routes.conversion_attribution import per_tool_conversions as _ptc
                    attr_conv = int((_ptc(days) or {}).get(tool, 0))
                except Exception:
                    attr_conv = 0
                entry["stages"]["5_converted"] = max(converted_signals, attr_conv)
                entry["converted_attributed"]  = attr_conv
                entry["distinct_callers"]      = distinct_callers
                # pair-code stages kept as supplementary context, NOT in the leak
                entry["pair_code_stages"] = {
                    "minted":  entry["stages"]["2_codes_minted"],
                    "viewed":  entry["stages"]["3_redeem_viewed"],
                    "clicked": entry["stages"]["4_stripe_clicked"],
                }

                # Drop rates on the HONEST funnel only: total calls -> distinct
                # paywall callers -> distinct converters. (The pair-code stages are
                # a separate flow that doesn't align per-tool, so they're excluded
                # from leak detection — that conflation was the false-positive.)
                s = entry["stages"]
                def _drop(a, b):
                    if a == 0: return None
                    return round(100.0 * (1 - (b / a)), 1)
                entry["drop_rates"] = {
                    "0_call_to_1_signal":      _drop(s["0_total_calls"],     s["1_paywall_signals"]),
                    "1_signal_to_5_converted": _drop(s["1_paywall_signals"], s["5_converted"]),
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
                    # r70 (2026-06-03): the old nested-ternary built keys like
                    # "0_total_calls_total_calls" / "0_call_total_calls" (string-
                    # munging the transition name), none of which exist in `s`, so
                    # starting_volume was ALWAYS None. Map the leading stage digit
                    # straight to the canonical count key instead.
                                        _stage_key_map = {
                        "0": "0_total_calls",
                        "1": "1_paywall_signals",
                        "2": "2_codes_minted",
                        "3": "3_redeem_viewed",
                        "4": "4_stripe_clicked",
                        "5": "5_converted",
                    }
                    entry["biggest_leak"] = {
                        "stage":         best_stage,
                        "drop_pct":      best_drop,
                        "starting_volume": s.get(_stage_key_map.get(best_stage[:1], "")),
                    }
                out.append(entry)
    finally:
        try: c.close()
        except Exception: pass
    return out


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
