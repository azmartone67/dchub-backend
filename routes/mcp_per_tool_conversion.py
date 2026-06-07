"""mcp_per_tool_conversion.py — Round 3 Unlock 1 (2026-06-07)
=============================================================

Per-tool MCP conversion analyzer + brain proposer for the 38 live MCP tools.

WHY
---
Round 1 + Round 2 closed the funnel BUG layer (3-strike claim, threshold→2,
per-platform copy, the /claim/<token> CF 404 + REST validator fix). The next
ceiling on conversion is signal quality: WHICH tools actually pull converters
through the funnel vs which ones just generate calls. Without per-tool
attribution we can't tell whether `analyze_site` (pro tier) is doing the
heavy lifting or whether the cheap `get_news` / `get_facility` free calls
account for 95% of our paywall hits with 0% close.

WHAT THIS SHIPS
---------------
Daily cron (02:00 UTC) reads mcp_call_log + mcp_upgrade_signals +
mcp_pair_codes + mcp_conversions, computes per-tool:

  - distinct_callers_30d   (DISTINCT api_key, exclude internal/probe)
  - calls_30d
  - paywall_hits_30d       (rows in mcp_upgrade_signals)
  - claims_minted_30d      (mcp_pair_codes rows with claim_token NOT NULL)
  - claims_used_30d        (mcp_pair_codes.email_at IS NOT NULL)
  - paid_conversions_30d   (joined to mcp_conversions.session_id)

Ranks by conversion rate (paid / callers).

WRITES TO BRAIN
---------------
For each LOW performer (>=100 distinct callers, 0 paid conversions):
  brain_findings.issue = 'mcp_tool_zero_conversion'
  detail = "<tool> · N callers / 0 paid · proposed action: ..."

For each HIGH performer (top 3 by conv rate, >=10 callers):
  brain_findings.issue = 'mcp_tool_high_converter'
  detail = "<tool> · X% conv rate · proposed action: promote in tools/list"

DASHBOARD SURFACE
-----------------
GET  /admin/per-tool-conversion          HTML ranking table (Cookie OR
                                          ?admin_key= OR X-Admin-Key)
GET  /api/v1/admin/per-tool-conversion   JSON (same auth)

Surfaced INLINE on /admin/funnel-health (separate card; not blocking
data fetch).

SAFETY
------
- Read-only. Only writes brain_findings; never touches mcp_dev_keys or
  tool tables.
- Kill switch: MCP_FUNNEL_R3_DISABLE=1
- In-process cache (10min) to prevent dashboard refresh storms.
- All probes savepoint-wrapped (one missing table never kills the rest).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from html import escape as _esc
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

mcp_per_tool_conversion_bp = Blueprint("mcp_per_tool_conversion", __name__)


# ── env ───────────────────────────────────────────────────────────────

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("ADMIN_KEY") or "").strip()

_DISABLE = os.environ.get("MCP_FUNNEL_R3_DISABLE", "").strip() in ("1", "true", "yes")

# Internal/probe traffic patterns to EXCLUDE from distinct_callers
# (these inflate the denominator and create fake 0% conv rates — same
# bug pattern documented in reference_dchub_mcp_signal_inflation.md).
# Keep 'mcp' (ambiguous, likely real anon traffic).
_INTERNAL_KEY_PATTERNS = ("dchub-%", "%-probe", "%-health", "%-scanner",
                          "loop%", "internal-%")

# Tool catalog — driven from routes.mcp_tool_catalog.TOOLS but lazily
# loaded so an import-order accident doesn't kill this module.
def _get_tools() -> list[tuple[str, str, str]]:
    """Return [(name, category, tier)] for the 38 MCP tools."""
    try:
        from routes.mcp_tool_catalog import TOOLS
        return [(t[0], t[1], t[2]) for t in TOOLS]
    except Exception as e:
        logger.warning("per_tool_conversion: tool catalog import failed: %s", e)
        return []


# Thresholds for brain proposals.
_LOW_CALLERS_FLOOR = 100   # need at least this many distinct callers
_HIGH_CALLERS_FLOOR = 10
_HIGH_RANK_LIMIT = 3        # top N high performers proposed


# ── DB ────────────────────────────────────────────────────────────────

def _conn():
    """Open a raw psycopg2 connection (matches funnel_health._conn pattern)."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        c = _pg.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("per_tool_conversion: db connect failed: %s", e)
        return None


def _savepoint(cur, name: str) -> bool:
    try:
        cur.execute(f"SAVEPOINT {name}")
        return True
    except Exception:
        return False


def _rollback_sp(cur, name: str) -> None:
    try:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    except Exception:
        pass


def _release_sp(cur, name: str) -> None:
    try:
        cur.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        pass


# ── Auth ──────────────────────────────────────────────────────────────

def _admin_ok(req) -> bool:
    sent = (req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    return bool(_ADMIN_KEY and sent == _ADMIN_KEY)


# ── 10-min in-process cache ───────────────────────────────────────────

_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_CACHE_TTL_S = 600


def _compute() -> dict:
    """Run all per-tool probes and assemble the ranking."""
    out: dict[str, Any] = {
        "tools": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_ok": False,
        "disabled": _DISABLE,
        "missing_tables": [],
        "low_performers": [],
        "high_performers": [],
        "total_tools": 0,
    }

    if _DISABLE:
        out["error"] = "MCP_FUNNEL_R3_DISABLE=1 — analyzer is off."
        return out

    tools = _get_tools()
    out["total_tools"] = len(tools)
    if not tools:
        out["error"] = "tool_catalog_import_failed"
        return out

    conn = _conn()
    if conn is None:
        return out
    out["db_ok"] = True

    try:
        cur = conn.cursor()

        # Build the internal-key exclusion clause once.
        excl_parts = " AND ".join(
            "api_key NOT LIKE %s" for _ in _INTERNAL_KEY_PATTERNS)

        # Per-tool probe loop. Each probe savepoint-wrapped so a missing
        # column on ONE table doesn't kill the whole analysis.
        for tool_name, category, tier in tools:
            row: dict[str, Any] = {
                "tool": tool_name,
                "category": category,
                "tier": tier,
                "distinct_callers_30d": 0,
                "calls_30d": 0,
                "paywall_hits_30d": 0,
                "claims_minted_30d": 0,
                "claims_used_30d": 0,
                "paid_conversions_30d": 0,
                "conv_rate_pct": 0.0,
                "engagement_score": 0,
            }

            # 1) distinct_callers + calls (mcp_call_log.tool)
            if _savepoint(cur, "ptc_calls"):
                try:
                    params = (tool_name,) + _INTERNAL_KEY_PATTERNS
                    cur.execute(
                        f"SELECT COUNT(DISTINCT api_key), COUNT(*) "
                        f"  FROM mcp_call_log "
                        f" WHERE tool = %s "
                        f"   AND timestamp >= NOW() - INTERVAL '30 days' "
                        f"   AND api_key IS NOT NULL "
                        f"   AND {excl_parts}",
                        params)
                    r = cur.fetchone()
                    if r:
                        row["distinct_callers_30d"] = int(r[0] or 0)
                        row["calls_30d"] = int(r[1] or 0)
                    _release_sp(cur, "ptc_calls")
                except Exception as e:
                    _rollback_sp(cur, "ptc_calls")
                    logger.debug("ptc_calls(%s) failed: %s", tool_name, e)
                    if "mcp_call_log" not in out["missing_tables"]:
                        out["missing_tables"].append("mcp_call_log")

            # 2) paywall_hits (mcp_upgrade_signals.tool_requested — Fix C
            #    restored this write 2026-06-06).
            if _savepoint(cur, "ptc_sig"):
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM mcp_upgrade_signals "
                        " WHERE tool_requested = %s "
                        "   AND created_at >= NOW() - INTERVAL '30 days'",
                        (tool_name,))
                    r = cur.fetchone()
                    if r:
                        row["paywall_hits_30d"] = int(r[0] or 0)
                    _release_sp(cur, "ptc_sig")
                except Exception as e:
                    _rollback_sp(cur, "ptc_sig")
                    logger.debug("ptc_sig(%s) failed: %s", tool_name, e)
                    if "mcp_upgrade_signals" not in out["missing_tables"]:
                        out["missing_tables"].append("mcp_upgrade_signals")

            # 3) claims_minted (mcp_pair_codes) — pair_codes have a
            #    tool_requested column populated by the paywall path.
            if _savepoint(cur, "ptc_minted"):
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM mcp_pair_codes "
                        " WHERE tool_requested = %s "
                        "   AND created_at >= NOW() - INTERVAL '30 days'",
                        (tool_name,))
                    r = cur.fetchone()
                    if r:
                        row["claims_minted_30d"] = int(r[0] or 0)
                    _release_sp(cur, "ptc_minted")
                except Exception as e:
                    _rollback_sp(cur, "ptc_minted")
                    logger.debug("ptc_minted(%s) failed: %s", tool_name, e)

            # 4) claims_used (mcp_pair_codes.email_at OR redeemed_at)
            if _savepoint(cur, "ptc_used"):
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM mcp_pair_codes "
                        " WHERE tool_requested = %s "
                        "   AND (email_at IS NOT NULL OR redeemed_at IS NOT NULL) "
                        "   AND created_at >= NOW() - INTERVAL '30 days'",
                        (tool_name,))
                    r = cur.fetchone()
                    if r:
                        row["claims_used_30d"] = int(r[0] or 0)
                    _release_sp(cur, "ptc_used")
                except Exception as e:
                    _rollback_sp(cur, "ptc_used")
                    logger.debug("ptc_used(%s) failed: %s", tool_name, e)

            # 5) paid_conversions — join mcp_pair_codes → mcp_conversions
            #    by session_id (Fix E wired client_reference_id=session_id).
            if _savepoint(cur, "ptc_paid"):
                try:
                    cur.execute(
                        "SELECT COUNT(DISTINCT c.id) FROM mcp_conversions c "
                        " JOIN mcp_pair_codes p ON p.session_id = c.session_id "
                        " WHERE p.tool_requested = %s "
                        "   AND c.created_at >= NOW() - INTERVAL '30 days'",
                        (tool_name,))
                    r = cur.fetchone()
                    if r:
                        row["paid_conversions_30d"] = int(r[0] or 0)
                    _release_sp(cur, "ptc_paid")
                except Exception as e:
                    _rollback_sp(cur, "ptc_paid")
                    logger.debug("ptc_paid(%s) failed: %s", tool_name, e)

            # Conv rate = paid / distinct_callers (the honest denominator —
            # the dchub MCP signal inflation note explicitly calls out the
            # COUNT(*)-as-denominator trap).
            if row["distinct_callers_30d"] > 0:
                row["conv_rate_pct"] = round(
                    100.0 * row["paid_conversions_30d"]
                    / row["distinct_callers_30d"], 3)

            # Engagement = a simple ordinal score for tiebreaking high-low
            # ranking when conv_rate_pct == 0: more callers AND more
            # paywall_hits ⇒ higher engagement; useful to find the tools
            # that ARE driving signal but NOT paying.
            row["engagement_score"] = (
                row["distinct_callers_30d"]
                + row["paywall_hits_30d"] * 2
                + row["claims_minted_30d"] * 5)

            out["tools"].append(row)

        # Rank: by conv_rate_pct DESC, then distinct_callers_30d DESC.
        out["tools"].sort(
            key=lambda r: (-r["conv_rate_pct"],
                           -r["distinct_callers_30d"],
                           -r["paywall_hits_30d"]))

        # ── LOW performers: >=100 callers + 0 paid in 30d.
        out["low_performers"] = [
            r for r in out["tools"]
            if (r["distinct_callers_30d"] >= _LOW_CALLERS_FLOOR
                and r["paid_conversions_30d"] == 0)
        ]

        # ── HIGH performers: top 3 by conv_rate_pct with >= floor callers.
        eligible_high = [
            r for r in out["tools"]
            if (r["distinct_callers_30d"] >= _HIGH_CALLERS_FLOOR
                and r["conv_rate_pct"] > 0)
        ]
        out["high_performers"] = eligible_high[:_HIGH_RANK_LIMIT]

    except Exception as e:
        logger.warning("per_tool_conversion: top-level probe failed: %s", e)
        out["error"] = str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return out


def _data_cached() -> dict:
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_S:
        return _CACHE["data"]
    d = _compute()
    _CACHE["data"] = d
    _CACHE["ts"] = now
    return d


# ── Brain findings ────────────────────────────────────────────────────

def _emit_brain_findings(data: dict) -> dict:
    """Write LOW + HIGH performer findings to brain_findings via the
    canonical upsert. Returns {low_written, high_written, errors}."""
    out = {"low_written": 0, "high_written": 0, "errors": []}
    if _DISABLE:
        out["errors"].append("disabled")
        return out

    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        out["errors"].append(f"writer_import: {e}")
        return out

    conn = _conn()
    if conn is None:
        out["errors"].append("no_db")
        return out

    try:
        cur = conn.cursor()

        # LOW performers — propose tighten/improve/retire actions.
        for r in data.get("low_performers", []):
            tool = r["tool"]
            callers = r["distinct_callers_30d"]
            paywall = r["paywall_hits_30d"]
            tier = r["tier"]
            # Choose the action heuristically:
            # - high paywall_hits + 0 paid ⇒ messaging/copy problem ⇒ improve desc
            # - low paywall_hits + 0 paid ⇒ tool isn't pulling weight ⇒ retire or tighten paywall
            if paywall >= 20:
                action = (
                    "Improve tool description (paywall hits but no closes — "
                    "messaging or paywall copy is the gating problem). Current "
                    "tier=%s." % tier)
            elif callers >= 200:
                action = (
                    "Tighten paywall — force trial earlier (high call volume, "
                    "low paywall hits suggests free tier is leaking value). "
                    "Current tier=%s." % tier)
            else:
                action = (
                    "Consider retiring or downgrading tier — tool generates "
                    "callers but converts nothing in 30d. Current tier=%s." % tier)
            detail = (
                f"{tool}: {callers} distinct callers · {paywall} paywall hits · "
                f"0 paid in 30d. Proposed action: {action}")
            try:
                upsert_brain_finding(
                    cur,
                    issue="mcp_tool_zero_conversion",
                    url=f"/admin/per-tool-conversion#{tool}",
                    count=callers,
                    detail=detail,
                    detector="mcp_per_tool_conversion",
                    status="open")
                out["low_written"] += 1
            except Exception as e:
                out["errors"].append(f"low/{tool}: {e}")

        # HIGH performers — propose visibility boost + cross-promo.
        for r in data.get("high_performers", []):
            tool = r["tool"]
            rate = r["conv_rate_pct"]
            callers = r["distinct_callers_30d"]
            paid = r["paid_conversions_30d"]
            detail = (
                f"{tool}: {rate}% conv rate · {paid} paid / {callers} callers. "
                f"Proposed action: promote in tools/list ordering (move to top "
                f"of category) + cross-promote on MCP Connect landing pages "
                f"(/integrations/mcp/cursor, /integrations/mcp/cline, etc).")
            try:
                upsert_brain_finding(
                    cur,
                    issue="mcp_tool_high_converter",
                    url=f"/admin/per-tool-conversion#{tool}",
                    count=int(rate * 100),  # store rate*100 as count for stable ordering
                    detail=detail,
                    detector="mcp_per_tool_conversion",
                    status="open")
                out["high_written"] += 1
            except Exception as e:
                out["errors"].append(f"high/{tool}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return out


# ── Render ────────────────────────────────────────────────────────────

def _fmt_n(v):
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _render_html(data: dict, admin_key: str) -> str:
    """Standalone HTML page that mirrors the funnel_health palette."""
    admin_key_safe = _esc(admin_key, quote=True)

    tools = data.get("tools") or []
    lows = data.get("low_performers") or []
    highs = data.get("high_performers") or []
    missing = data.get("missing_tables") or []
    disabled = data.get("disabled")

    # Table rows — color-code by conv_rate_pct (green/amber/red).
    rows_html = []
    for r in tools:
        rate = r["conv_rate_pct"]
        if rate >= 1.0:
            color = "#22c55e"
        elif rate > 0:
            color = "#f59e0b"
        elif r["distinct_callers_30d"] >= _LOW_CALLERS_FLOOR:
            color = "#ef4444"  # high-volume zero — that's the alarm
        else:
            color = "#94a3b8"
        rows_html.append(
            f'<tr>'
            f'  <td>{_esc(r["tool"])}</td>'
            f'  <td><span class="tier tier-{_esc(r["tier"])}">{_esc(r["tier"])}</span></td>'
            f'  <td class="num">{_fmt_n(r["calls_30d"])}</td>'
            f'  <td class="num">{_fmt_n(r["distinct_callers_30d"])}</td>'
            f'  <td class="num">{_fmt_n(r["paywall_hits_30d"])}</td>'
            f'  <td class="num">{_fmt_n(r["claims_minted_30d"])}</td>'
            f'  <td class="num">{_fmt_n(r["claims_used_30d"])}</td>'
            f'  <td class="num">{_fmt_n(r["paid_conversions_30d"])}</td>'
            f'  <td class="num" style="color:{color};font-weight:600">{rate:.2f}%</td>'
            f'</tr>'
        )
    table_html = "\n".join(rows_html) or (
        '<tr><td colspan="9" class="empty">No data yet</td></tr>')

    # Low/high lists.
    low_html = "".join(
        f'<li><strong>{_esc(r["tool"])}</strong> · {_fmt_n(r["distinct_callers_30d"])} callers · '
        f'{_fmt_n(r["paywall_hits_30d"])} paywall hits · 0 paid</li>'
        for r in lows
    ) or '<li class="empty">No tools at zero-conv threshold (≥100 callers).</li>'

    high_html = "".join(
        f'<li><strong>{_esc(r["tool"])}</strong> · {r["conv_rate_pct"]:.2f}% · '
        f'{_fmt_n(r["paid_conversions_30d"])} paid / {_fmt_n(r["distinct_callers_30d"])} callers</li>'
        for r in highs
    ) or '<li class="empty">No high-converter tools yet.</li>'

    missing_chip = ""
    if missing:
        missing_chip = (
            f'<div style="background:#fbbf2422;color:#fbbf24;padding:6px 12px;'
            f'border-radius:4px;font-size:11px;margin-bottom:12px;">'
            f'missing tables: {_esc(", ".join(missing))}</div>')

    disabled_chip = ""
    if disabled:
        disabled_chip = (
            '<div style="background:#ef444422;color:#ef4444;padding:6px 12px;'
            'border-radius:4px;font-size:11px;margin-bottom:12px;">'
            'analyzer disabled · MCP_FUNNEL_R3_DISABLE=1</div>')

    return f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Per-tool conversion · DC Hub admin</title>
  <link rel="stylesheet" href="/dchub-brand.css">
  <style>
    :root{{--bg:#0a0e1a;--bg-card:#0e1424;--fg:#e6ecf5;--muted:#94a3b8;
          --accent:#3da9fc;--border:#1c2942;}}
    body{{background:var(--bg);color:var(--fg);
          font-family:'Inter',system-ui,sans-serif;
          margin:0;padding:32px;}}
    .wrap{{max-width:1280px;margin:0 auto;}}
    h1{{font-size:22px;margin:0 0 6px;}}
    h2{{font-size:15px;margin:0 0 12px;color:var(--fg);}}
    .sub{{color:var(--muted);font-size:12px;margin-bottom:24px;}}
    .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px;}}
    .card{{background:var(--bg-card);border:1px solid var(--border);
           border-radius:8px;padding:16px;}}
    .card ul{{margin:0;padding-left:18px;font-size:12px;color:var(--fg);line-height:1.6;}}
    .card li{{margin-bottom:4px;}}
    .card li.empty{{color:var(--muted);list-style:none;margin-left:-18px;}}
    table{{width:100%;border-collapse:collapse;font-size:12px;}}
    th{{background:#0a0e1a;color:var(--muted);text-align:left;
        padding:8px 10px;border-bottom:1px solid var(--border);
        font-weight:600;font-size:10px;text-transform:uppercase;}}
    td{{padding:8px 10px;border-bottom:1px solid var(--border);}}
    td.num{{text-align:right;font-variant-numeric:tabular-nums;}}
    td.empty{{text-align:center;color:var(--muted);padding:24px;}}
    .tier{{font-size:10px;padding:2px 6px;border-radius:3px;font-weight:600;
           text-transform:uppercase;}}
    .tier-free{{background:#94a3b822;color:#94a3b8;}}
    .tier-identified{{background:#3da9fc22;color:#3da9fc;}}
    .tier-pro{{background:#a78bfa22;color:#a78bfa;}}
    .footer{{font-size:10px;color:var(--muted);margin-top:24px;text-align:center;}}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Per-tool conversion ranking</h1>
    <div class="sub">
      Last 30d · {_fmt_n(data.get("total_tools", 0))} tools probed ·
      generated {_esc((data.get("generated_at") or "")[:19])}Z
    </div>
    {disabled_chip}
    {missing_chip}

    <div class="grid2">
      <div class="card">
        <h2>Low performers <span style="font-size:10px;color:#ef4444;">need attention</span></h2>
        <ul>{low_html}</ul>
      </div>
      <div class="card">
        <h2>High performers <span style="font-size:10px;color:#22c55e;">promote</span></h2>
        <ul>{high_html}</ul>
      </div>
    </div>

    <div class="card">
      <h2>Per-tool ranking (sorted by conv rate DESC)</h2>
      <table>
        <thead><tr>
          <th>Tool</th>
          <th>Tier</th>
          <th style="text-align:right">Calls</th>
          <th style="text-align:right">Distinct callers</th>
          <th style="text-align:right">Paywall hits</th>
          <th style="text-align:right">Claims minted</th>
          <th style="text-align:right">Claims used</th>
          <th style="text-align:right">Paid</th>
          <th style="text-align:right">Conv rate</th>
        </tr></thead>
        <tbody>{table_html}</tbody>
      </table>
    </div>

    <div class="footer">
      cache_ttl={_CACHE_TTL_S}s · routes/mcp_per_tool_conversion.py ·
      /admin/per-tool-conversion · /api/v1/admin/per-tool-conversion ·
      brain detector daily 02:00 UTC ·
      <a href="/admin/funnel-health?admin_key={admin_key_safe}" style="color:var(--accent);">funnel-health</a>
    </div>
  </div>
</body>
</html>"""


# ── Endpoints ─────────────────────────────────────────────────────────

@mcp_per_tool_conversion_bp.route(
    "/admin/per-tool-conversion", methods=["GET"])
@mcp_per_tool_conversion_bp.route(
    "/api/v1/admin/per-tool-conversion", methods=["GET"])
def admin_per_tool_conversion():
    """Dual-route: canonical + CF zone-worker bypass alias (mirrors the
    /admin/funnel-health pattern)."""
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401

    data = _data_cached()
    if (request.args.get("format") or "").lower() == "json":
        return jsonify(data), 200

    admin_key = (request.headers.get("X-Admin-Key")
                 or request.args.get("admin_key") or "").strip()
    html = _render_html(data, admin_key)
    return Response(html, mimetype="text/html")


@mcp_per_tool_conversion_bp.route(
    "/api/v1/admin/per-tool-conversion/run", methods=["POST"])
def admin_per_tool_conversion_run():
    """Force a fresh compute + emit brain findings. Called by the daily
    02:00 UTC cron (per_tool_conversion in crawler_scheduler.SCHEDULE).
    Idempotent — brain_findings_writer.upsert_brain_finding updates the
    existing row (or inserts once) so re-runs don't flood findings."""
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    # Bust cache so we get fresh data.
    _CACHE["data"] = None
    _CACHE["ts"] = 0.0
    data = _data_cached()
    brain_result = _emit_brain_findings(data)
    return jsonify(
        ok=True,
        total_tools=data.get("total_tools", 0),
        low_performers=len(data.get("low_performers") or []),
        high_performers=len(data.get("high_performers") or []),
        brain_findings=brain_result,
        generated_at=data.get("generated_at"),
    ), 200


@mcp_per_tool_conversion_bp.route(
    "/api/v1/admin/per-tool-conversion/top", methods=["GET"])
def admin_per_tool_conversion_top():
    """Compact JSON used by /admin/funnel-health's inline card so the
    funnel-health page doesn't have to re-run the full 38-tool probe
    every time the operator hits refresh. Just the top 3 + bottom 3
    by conv rate."""
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    data = _data_cached()
    tools = data.get("tools") or []
    return jsonify(
        ok=True,
        top_3=tools[:3],
        bottom_3=[r for r in tools if r["distinct_callers_30d"] > 0][-3:],
        generated_at=data.get("generated_at"),
    ), 200
