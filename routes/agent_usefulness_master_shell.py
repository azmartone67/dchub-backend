"""
agent_usefulness_master_shell.py — Agent-legibility / usefulness master shell (2026-07-02).
=============================================================================================

WHY
---
Three AI models (Gemini, Perplexity, Grok) independently flagged the same thing:
DC Hub *has* the data, but it isn't maximally LEGIBLE to an AI agent. The specific
gripes were concrete and measurable — under-described tool params, missing output
schemas, poisoned integer bounds (±9007199254740991), DCPI values that look cloned
across every market in an ISO, sparse null-heavy grid columns, and GEO surfaces that
don't advertise the new fields. This shell MEASURES exactly those things and persists
a single 0-100 `agent_usefulness_score` so the trend is trackable on a tick.

WHAT
----
POST /api/v1/admin/agent-usefulness/master-tick runs three tiers:
  · TIER 1 — MEASURE:  live MCP tools/list schema completeness, DCPI de-clone score,
                       null-field density, GEO surface legibility flags.
  · TIER 2 — SCORE:    weighted 0-100 blend + top-3 gaps with a one-line fix hint.
  · TIER 3 — PERSIST:  one snapshot row per tick (agent_usefulness_snapshots).
GET /api/v1/admin/agent-usefulness/state returns the latest snapshot + trend.

MEASURES-AND-REPORTS ONLY. It never mutates production data — the ONLY write is its
own snapshot table (created via a direct cursor execute + commit, because db_utils
safe_db SILENTLY SKIPS DDL).

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY). Fail-closed.
Kill switch: AGENT_USEFULNESS_MASTER_DISABLED=1.
"""
from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

agent_usefulness_master_shell_bp = Blueprint("agent_usefulness_master_shell", __name__)

_BASE = os.environ.get(
    "DCHUB_BACKEND_BASE",
    "https://dchub-backend-production.up.railway.app",
)
# The MCP server is a SEPARATE service (server.mjs), NOT the Flask backend —
# the backend base does not serve the MCP protocol. tools/list must hit the
# canonical public endpoint (dchub.cloud/mcp), else _mcp_tools() gets 0 tools
# and the schema components score 0 against a perfectly-described live server.
_MCP_ENDPOINT = os.environ.get("DCHUB_MCP_ENDPOINT", "https://dchub.cloud/mcp")
_INT_MIN = -9007199254740991
_INT_MAX = 9007199254740991


# ── auth (mirrors audience_master_shell) ──────────────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("AGENT_USEFULNESS_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


# ── self-probe HTTP helper (mirrors audience_master_shell._call) ──────
def _call(path: str, timeout: int = 20) -> tuple[int, str]:
    url = _BASE.rstrip("/") + path
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-DC-Probe", "agent-usefulness-tick")  # rate-limiter bypass
        req.add_header("User-Agent", "dchub-agent-usefulness-orchestrator/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _mcp_tools(timeout: int = 20) -> list[dict]:
    """Live tools/list over /mcp — initialize then tools/list, parse SSE data: lines."""
    hdr = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "dchub-usefulness-probe", "version": "1"}}}).encode()
    req = urllib.request.Request(_MCP_ENDPOINT, data=init, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id")
    hdr2 = dict(hdr)
    if sid:
        hdr2["mcp-session-id"] = sid
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    req2 = urllib.request.Request(_MCP_ENDPOINT, data=body, headers=hdr2)
    with urllib.request.urlopen(req2, timeout=timeout) as r:
        out = r.read().decode("utf-8", "replace")
    for ln in out.splitlines():
        if ln.startswith("data: "):
            ln = ln[6:]
        if ln.startswith("{") and '"id":2' in ln.replace(" ", ""):
            d = json.loads(ln)
            return (d.get("result") or {}).get("tools", []) or []
    return []


# ── DB (pooled canon conn — DDL via direct cursor, safe_db SKIPS DDL) ─
def _pct(numer: int, denom: int) -> float | None:
    return round(100.0 * numer / denom, 2) if denom else None


def _close(conn) -> None:
    """Roll back (read-only — never leave a tx open) + return the pooled conn."""
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        from main import return_pg_connection
        return_pg_connection(conn)
    except Exception:
        pass


# ── TIER 1 — MEASURE ──────────────────────────────────────────────────
def _measure_schema() -> dict:
    """1a. Tool-schema completeness from the live MCP tools/list."""
    try:
        tools = _mcp_tools()
    except Exception:
        tools = []
    total = len(tools)
    if not total:
        return {"total_tools": 0, "tools_with_all_params_described_pct": None,
                "tools_with_output_schema": None, "params_with_bad_int_bounds": None}
    all_described = 0
    with_output = 0
    bad_bounds = 0
    for t in tools:
        try:
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            if props:
                if all((str((p or {}).get("description") or "").strip()) for p in props.values()):
                    all_described += 1
            else:
                all_described += 1  # no params → trivially fully described
            if t.get("outputSchema") is not None:
                with_output += 1
            for p in props.values():
                p = p or {}
                if str(p.get("type")) == "integer":
                    if p.get("minimum") == _INT_MIN or p.get("maximum") == _INT_MAX:
                        bad_bounds += 1
        except Exception:
            continue
    return {
        "total_tools": total,
        "tools_with_all_params_described_pct": _pct(all_described, total),
        "tools_with_output_schema": with_output,
        "params_with_bad_int_bounds": bad_bounds,
    }


def _measure_db() -> dict:
    """1b. DCPI de-clone score + 1c. null-field density (fail-soft, read-only)."""
    out = {"iso_clone_ratio": None, "worst_cloned_isos": [],
           "null_density": {}}
    conn = None
    try:
        from main import get_pg_connection, return_pg_connection
    except Exception:
        return out
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
        except Exception:
            pass
        # 1b — DCPI de-clone: distinct time_to_power / market_count per ISO (>=3 markets)
        try:
            cur.execute("""
                SELECT iso,
                       COUNT(*) AS mkts,
                       COUNT(DISTINCT time_to_power_months) AS d_ttp,
                       COUNT(DISTINCT excess_power_score)   AS d_excess,
                       COUNT(DISTINCT constraint_score)     AS d_composite
                FROM market_power_scores
                WHERE iso IS NOT NULL AND iso <> ''
                GROUP BY iso
                HAVING COUNT(*) >= 3
            """)
            rows = cur.fetchall() or []
            ratios = []
            per_iso = []
            for iso, mkts, d_ttp, d_excess, d_comp in rows:
                mkts = mkts or 0
                r = (float(d_ttp or 0) / mkts) if mkts else 0.0
                ratios.append(r)
                per_iso.append({"iso": iso, "markets": mkts,
                                "distinct_time_to_power": d_ttp,
                                "distinct_excess_power_score": d_excess,
                                "distinct_composite": d_comp,
                                "clone_ratio": round(r, 3)})
            if ratios:
                out["iso_clone_ratio"] = round(sum(ratios) / len(ratios), 3)
            # worst = lowest ratio, then most markets
            per_iso.sort(key=lambda x: (x["clone_ratio"], -x["markets"]))
            out["worst_cloned_isos"] = per_iso[:5]
        except Exception:
            pass
        # 1c — null-field density on market_power_scores
        try:
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE queue_wait_months     IS NULL),
                       COUNT(*) FILTER (WHERE reserve_margin_pct    IS NULL),
                       COUNT(*) FILTER (WHERE curtailment_pct       IS NULL),
                       COUNT(*) FILTER (WHERE gen_additions_12mo_mw IS NULL)
                FROM market_power_scores
            """)
            row = cur.fetchone() or (0, 0, 0, 0, 0)
            tot = row[0] or 0
            out["null_density"] = {
                "row_count": tot,
                "queue_wait_months_pct_null": _pct(row[1] or 0, tot),
                "reserve_margin_pct_pct_null": _pct(row[2] or 0, tot),
                "curtailment_pct_pct_null": _pct(row[3] or 0, tot),
                "gen_additions_12mo_mw_pct_null": _pct(row[4] or 0, tot),
            }
        except Exception:
            pass
    except Exception:
        pass
    finally:
        _close(conn)
    return out


def _measure_surfaces() -> dict:
    """1d. Surface legibility flags via fail-soft HTTP GETs."""
    flags = {"dcpi_has_time_to_power": False, "llms_txt_has_returns": False}
    try:
        code, html = _call("/dcpi/ashburn-va")
        flags["dcpi_has_time_to_power"] = bool(code == 200 and "Time to Power" in html)
    except Exception:
        pass
    try:
        code, txt = _call("/llms.txt")
        flags["llms_txt_has_returns"] = bool(code == 200 and "returns" in txt.lower())
    except Exception:
        pass
    return flags


def tier1_measure() -> dict:
    return {
        "schema": _measure_schema(),
        "db": _measure_db(),
        "surfaces": _measure_surfaces(),
    }


# ── TIER 2 — SCORE ────────────────────────────────────────────────────
_WEIGHTS = {
    "schema_completeness": 30,   # pct of tools with all params described
    "no_bad_bounds": 10,         # 0 poisoned integer bounds
    "has_output_schemas": 10,    # pct of tools with an outputSchema
    "dcpi_differentiation": 25,  # iso_clone_ratio * 100
    "low_null_density": 15,      # 100 - avg pct_null across grid cols
    "surface_legibility": 10,    # 2 legibility flags
}
_FIX_HINTS = {
    "schema_completeness": "Add a non-empty description to every tool inputSchema property.",
    "no_bad_bounds": "Replace ±9007199254740991 integer bounds with real min/max.",
    "has_output_schemas": "Attach an outputSchema to each tool so agents know the return shape.",
    "dcpi_differentiation": "Stop cloning ISO-level DCPI values to every market — compute per-market.",
    "low_null_density": "Backfill queue_wait / reserve_margin / curtailment / gen_additions columns.",
    "surface_legibility": "Advertise new fields on /dcpi HTML (Time to Power) + /llms.txt (returns:).",
}


def tier2_score(m: dict) -> dict:
    sc = m.get("schema") or {}
    db = m.get("db") or {}
    surf = m.get("surfaces") or {}
    total = sc.get("total_tools") or 0

    described_pct = sc.get("tools_with_all_params_described_pct")
    comp_schema = (described_pct / 100.0) if described_pct is not None else 0.0

    bad = sc.get("params_with_bad_int_bounds")
    comp_bounds = 1.0 if (bad == 0) else (0.0 if bad else 0.0)

    outs = sc.get("tools_with_output_schema")
    comp_output = (outs / total) if (total and outs is not None) else 0.0

    clone = db.get("iso_clone_ratio")
    comp_dcpi = clone if (clone is not None) else 0.0

    nd = db.get("null_density") or {}
    nulls = [nd.get("queue_wait_months_pct_null"), nd.get("reserve_margin_pct_pct_null"),
             nd.get("curtailment_pct_pct_null"), nd.get("gen_additions_12mo_mw_pct_null")]
    nulls = [x for x in nulls if x is not None]
    comp_null = (1.0 - (sum(nulls) / len(nulls)) / 100.0) if nulls else 0.0

    flags = [bool(surf.get("dcpi_has_time_to_power")), bool(surf.get("llms_txt_has_returns"))]
    comp_surface = sum(1 for f in flags if f) / len(flags) if flags else 0.0

    frac = {
        "schema_completeness": max(0.0, min(1.0, comp_schema)),
        "no_bad_bounds": max(0.0, min(1.0, comp_bounds)),
        "has_output_schemas": max(0.0, min(1.0, comp_output)),
        "dcpi_differentiation": max(0.0, min(1.0, comp_dcpi)),
        "low_null_density": max(0.0, min(1.0, comp_null)),
        "surface_legibility": max(0.0, min(1.0, comp_surface)),
    }
    components = {k: round(frac[k] * _WEIGHTS[k], 2) for k in _WEIGHTS}
    score = round(sum(components.values()), 1)

    # top 3 gaps = lowest earned-fraction-of-weight components
    ranked = sorted(_WEIGHTS, key=lambda k: components[k] / _WEIGHTS[k])
    top_gaps = [{"component": k,
                 "earned": components[k], "max": _WEIGHTS[k],
                 "fix": _FIX_HINTS[k]} for k in ranked[:3]]

    return {"score": score, "components": components, "weights": dict(_WEIGHTS),
            "top_gaps": top_gaps}


# ── TIER 3 — PERSIST (direct cursor DDL + INSERT; read-only elsewhere) ─
def _persist(score: float | None, components: dict, measures: dict) -> bool:
    conn = None
    try:
        from main import get_pg_connection, return_pg_connection
    except Exception:
        return False
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_usefulness_snapshots (
                id              BIGSERIAL PRIMARY KEY,
                score           REAL,
                components_json JSONB,
                measures_json   JSONB,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(
            "INSERT INTO agent_usefulness_snapshots (score, components_json, measures_json) "
            "VALUES (%s, %s, %s)",
            (score, json.dumps(components or {}), json.dumps(measures or {})),
        )
        conn.commit()
        return True
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        # NOTE: commit already ran on success; _close's rollback is a harmless no-op then.
        _close(conn)


# ── endpoints ─────────────────────────────────────────────────────────
@agent_usefulness_master_shell_bp.route(
    "/api/v1/admin/agent-usefulness/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="AGENT_USEFULNESS_MASTER_DISABLED"), 200
    started = time.time()
    measures = tier1_measure()
    scored = tier2_score(measures)
    persisted = _persist(scored.get("score"), scored.get("components"), measures)
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        score=scored.get("score"),
        components=scored.get("components"),
        measures=measures,
        top_gaps=scored.get("top_gaps"),
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@agent_usefulness_master_shell_bp.route(
    "/api/v1/admin/agent-usefulness/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = None
    try:
        from main import get_pg_connection, return_pg_connection
    except Exception:
        return jsonify(error="db_unavailable"), 503
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
        except Exception:
            pass
        latest = None
        try:
            cur.execute(
                "SELECT id, score, components_json, measures_json, created_at "
                "FROM agent_usefulness_snapshots ORDER BY id DESC LIMIT 1")
            r = cur.fetchone()
            if r:
                latest = {"id": r[0], "score": r[1], "components": r[2],
                          "measures": r[3], "created_at": str(r[4])}
        except Exception:
            pass
        trend = []
        try:
            cur.execute(
                "SELECT score, created_at FROM agent_usefulness_snapshots "
                "ORDER BY id DESC LIMIT 14")
            trend = [{"score": row[0], "created_at": str(row[1])}
                     for row in (cur.fetchall() or [])]
            trend.reverse()
        except Exception:
            pass
        return jsonify(latest=latest, trend=trend, count=len(trend)), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:120]}"), 500
    finally:
        _close(conn)
