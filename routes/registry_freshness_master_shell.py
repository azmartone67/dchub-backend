"""
routes/registry_freshness_master_shell.py — Registry Freshness Master Shell (2026-07-06).

The sentinel that was MISSING when Glama's listing sat at a stale 33 for weeks
while our live server served far more — nobody was watching whether the external MCP-registry
listings still matched our canonical tool count. This pane flags that drift the
moment the presence crawler observes it, so a stale listing surfaces on a
scoreboard instead of by accident.

Three lanes:
  1. CANONICAL CONSISTENCY — our single-source-of-truth (ai_surface_canon PINNED)
     is defined AND agrees with the count the presence crawler resolved LIVE
     (our_actual_tool_count). Catches an internally-stale canon (the exact class
     of bug where PINNED said 53 while live tools/list was 58).
  2. REGISTRY PARITY — from mcp_presence_listings (what the crawler last scraped
     off each registry): no tracked registry is showing a drifted tool count.
     RED here = a Glama/Smithery/PulseMCP listing has gone stale. (Note: the
     crawler captures tool COUNT only, not a version string, so parity is
     count-based.)
  3. CRAWLER + PUSH HEALTH — the presence crawler ran recently (<48h), open
     drift findings are surfaced, and the push posture is shown HONESTLY:
     REGISTRY_SUBMIT_ENABLED is off by default, so we're WATCH-ONLY — the brain
     detects drift but does not autonomously push a fix. That gap is the point.

★ PURE-DB shell: every probe is a Neon query, an env read, or a pure-Python
import of the PINNED constant — NO outbound HTTP (never call _mcp_tool_count /
resolve_canon / _our_actual_tool_count from here; those hit the network and are
the self-request pattern that caused the 07-06 flywheel outage). Disabled state
returns 404, NOT 5xx (a 5xx trips the CF failover breaker → stale Render — see
[[reference_dchub_flywheel_master_shell]]).

Endpoints:
  GET/POST /api/v1/admin/registry-freshness/master-tick   JSON scoreboard
  GET      /admin/registry-freshness                       HTML dashboard
  GET      /api/v1/admin/registry-freshness                CF zone-worker bypass

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (→ DCHUB_INTERNAL_KEY).
Kill: REGISTRY_FRESHNESS_DISABLED=1.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

registry_freshness_master_shell_bp = Blueprint("registry_freshness_master_shell", __name__)

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("REGISTRY_FRESHNESS_DISABLED") or "").strip() == "1"


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# ── db helpers (pure-DB; no network) ──────────────────────────────────

def _conn():
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[regfresh] db connect failed: %s", e)
        return None


def _scalar(c, sql: str):
    """Fail-soft scalar. None on error (NOT 0). Literal SQL only, NO params tuple
    — the drift queries carry a literal % (LIKE 'mcp_presence_%'); an empty ()
    would trigger %-substitution (the empty-tuple % trap)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug("[regfresh] scalar failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _rows(c, sql: str) -> list:
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[regfresh] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


def _check(cid: str, name: str, passed, detail: str) -> dict:
    return {"id": cid, "name": name, "pass": passed, "detail": (detail or "")[:300]}


def _pinned():
    """Pure-Python import of the canon constant — no network. (None on failure.)"""
    try:
        from ai_surface_canon import PINNED
        return PINNED
    except Exception as e:
        logger.debug("[regfresh] PINNED import failed: %s", e)
        return None


# ── lane 1 · canonical consistency ────────────────────────────────────

def _lane_canonical(c) -> list[dict]:
    out = []
    p = _pinned()
    if p is None:
        return [_check("rf_canon_load", "ai_surface_canon PINNED loads", None,
                       "PINNED import failed")]
    canon_tools = p.get("tools_advertised")
    canon_ver = p.get("version")

    # 1a — the canon is defined (this is the reference everything compares to).
    out.append(_check("rf_canon_defined", "canonical tool-count + version pinned",
                      (canon_tools is not None and bool(canon_ver)),
                      f"canon = {canon_tools} tools / v{canon_ver}"))

    # 1b — the canon agrees with what the crawler resolved LIVE (our_actual_tool_count).
    # If PINNED lags the live tools/list (the 53-vs-58 bug), this goes RED.
    if c is None:
        out.append(_check("rf_canon_vs_live", "pinned count == crawler live-resolved count",
                          None, "no db"))
    else:
        live = _scalar(c, "SELECT MAX(our_actual_tool_count) FROM mcp_presence_listings "
                          "WHERE our_actual_tool_count IS NOT NULL")
        if live is None or canon_tools is None:
            out.append(_check("rf_canon_vs_live", "pinned count == crawler live-resolved count",
                              None, f"pinned={canon_tools} crawler-live={live}"))
        else:
            out.append(_check("rf_canon_vs_live", "pinned count == crawler live-resolved count",
                              int(live) == int(canon_tools),
                              f"pinned={canon_tools} vs crawler-live={live}"))
    return out


# ── lane 2 · registry parity ──────────────────────────────────────────

def _lane_parity(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("rf_nodb", "registry parity needs db", None, "no db")]

    # 2a — no tracked (seeded) registry is showing a drifted tool count. The
    # crawler sets drift_detected = (observed_published_tools != our_actual_count).
    # This is the auto-detector: Glama observed 33 vs our 58 → drift → RED.
    ndrift = _scalar(c, "SELECT count(*) FROM mcp_presence_listings "
                        "WHERE COALESCE(drift_detected, FALSE) = TRUE "
                        "AND COALESCE(discovered, FALSE) = FALSE")
    detail = "no drifted registries"
    if ndrift:
        drifted = _rows(c, "SELECT registry_name, dchub_metric_published_tools, our_actual_tool_count "
                           "FROM mcp_presence_listings "
                           "WHERE COALESCE(drift_detected, FALSE) = TRUE "
                           "AND COALESCE(discovered, FALSE) = FALSE "
                           "ORDER BY registry_name LIMIT 8")
        detail = "drifted: " + ", ".join(
            f"{r[0]}(shows {r[1]} vs {r[2]})" for r in drifted)
    out.append(_check("rf_registry_drift", "no registry showing a drifted tool count",
                      (ndrift == 0) if ndrift is not None else None, detail))

    # 2b — GAUGE: listings the registry itself hasn't refreshed in >30d (their
    # own crawl cadence, not ours). Informational.
    stale = _scalar(c, "SELECT count(*) FROM mcp_presence_listings "
                       "WHERE COALESCE(dchub_metric_stale_days, 0) > 30 "
                       "AND COALESCE(discovered, FALSE) = FALSE")
    out.append(_check("rf_registry_stale_listings", "registry listings not refreshed >30d (their side)",
                      None, f"{stale} listing(s) stale >30d" if stale is not None else "query failed"))

    # 2c — GAUGE: how many tracked registries we're monitoring.
    tracked = _scalar(c, "SELECT count(*) FROM mcp_presence_listings "
                         "WHERE COALESCE(discovered, FALSE) = FALSE")
    out.append(_check("rf_registries_tracked", "tracked registries monitored",
                      None, f"{tracked} registries in presence table" if tracked is not None else "query failed"))
    return out


# ── lane 3 · crawler + push health ────────────────────────────────────

def _lane_ops(c) -> list[dict]:
    out = []

    # 3a — presence crawler ran recently. Durable freshness = newest
    # last_crawled_at (crawler_run_claims is deleted on clean release, so it's
    # not a reliable signal).
    if c is not None:
        age = _scalar(c, "SELECT EXTRACT(EPOCH FROM (now() - MAX(last_crawled_at))) "
                         "FROM mcp_presence_listings")
        out.append(_check("rf_crawler_fresh", "presence crawler ran < 48h ago",
                          (float(age) < 172800) if age is not None else None,
                          f"last crawl {round(float(age)/3600,1)}h ago" if age is not None else "no crawl rows"))

        # 3b — GAUGE: open presence/registry drift findings the brain has filed.
        nfind = _scalar(c, "SELECT count(*) FROM brain_findings "
                           "WHERE COALESCE(status,'open') NOT IN ('resolved','wont_fix','dismissed') "
                           "AND resolved_at IS NULL "
                           "AND (issue LIKE 'mcp_presence_%' OR issue LIKE 'mcp_registry_%')")
        out.append(_check("rf_open_drift_findings", "open registry/presence findings",
                          None, f"{nfind} open finding(s)" if nfind is not None else "query failed"))
    else:
        out.append(_check("rf_crawler_fresh", "presence crawler ran < 48h ago", None, "no db"))
        out.append(_check("rf_open_drift_findings", "open registry/presence findings", None, "no db"))

    # 3c — GAUGE: push posture, told HONESTLY. REGISTRY_SUBMIT_ENABLED is off by
    # default → the brain WATCHES drift but does not autonomously PUSH a fix.
    # (Even with the distribution shell act-enabled, the network POST bottoms
    # out at this flag.) This is the systemic gap the whole 07-06 registry
    # firefight came from.
    push_on = _env_truthy("REGISTRY_SUBMIT_ENABLED")
    dry = _env_truthy("REGISTRY_SUBMIT_DRY_RUN")
    out.append(_check("rf_push_posture", "registry push posture",
                      None,
                      ("push=WIRED (REGISTRY_SUBMIT_ENABLED on"
                       + (", DRY_RUN" if dry else "") + ")")
                      if push_on else
                      "push=WATCH-ONLY — REGISTRY_SUBMIT_ENABLED off; brain detects drift, does not auto-push"))
    return out


# ── tick orchestration ────────────────────────────────────────────────
# (key, label, fn, actuator) — actuator NAMED but never fired (diagnostic).

_LANES = [
    ("canonical", "1 · Canonical consistency",   _lane_canonical,
     "ai_surface_canon PINNED + re-sync backend/worker discovery surfaces"),
    ("parity",    "2 · Registry parity",         _lane_parity,
     "manual listing edit (Glama Edit-listing) / trigger registry re-crawl"),
    ("ops",       "3 · Crawler + push health",   _lane_ops,
     "wire REGISTRY_SUBMIT_ENABLED + real push targets (mcp-registry/submit-all)"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 30.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS registry_freshness_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[regfresh] snapshot ddl skipped: %s", e)


def _escalate_red(c, lanes) -> None:
    """r-actuate (2026-07-17): the shell was pure-DETECT — it snapshotted and filed
    NOTHING, so registry drift died in a table nobody reads. On a RED lane it now
    ESCALATES: upserts ONE brain_finding the operator/brain actually sees. NO
    outbound HTTP (preserves the pure-DB, no-self-request invariant that exists
    because inline pushes caused the 07-06 flywheel outage) — the actual push stays
    owner-gated (REGISTRY_SUBMIT_ENABLED + the daily crawler auto-fix). This closes
    the DETECT->ESCALATE half only. Plain UPDATE-or-INSERT (NOT the savepoint-wrapped
    canonical writer, which no-ops under an autocommit conn); auto-resolves on green
    so the finding list stays honest. Fail-soft; never raises."""
    if c is None:
        return
    red = [l for l in lanes if not l["pass"]]
    try:
        with c.cursor() as cur:
            if not red:
                cur.execute("UPDATE brain_findings SET status='resolved', resolved_at=now() "
                            "WHERE issue=%s AND status='open'", ("registry_freshness_red",))
            else:
                bits = []
                for l in red:
                    for ch in [x for x in l["checks"] if x["pass"] is False][:4]:
                        bits.append(f"[{l['lane']}] {ch['name']}: {ch['detail']}")
                detail = (f"Registry freshness RED — {len(red)}/{len(lanes)} lane(s) failing. "
                          + " || ".join(bits))[:740] + " | See /admin/registry-freshness."
                # ★2026-08-29 lane 8: canonical writer. This site hand-rolled
                # the whole UPDATE-then-INSERT dance that
                # upsert_brain_finding already does constraint-agnostically,
                # and its `count = COALESCE(count,1)+1` bumped a tally on
                # EVERY tick — the scan-cycle inflation the episode ledger
                # was built to stop (max observed: 1463 on one finding).
                from routes.brain_findings_writer import upsert_brain_finding
                upsert_brain_finding(
                    cur,
                    issue="registry_freshness_red",
                    url="https://dchub.cloud/admin/registry-freshness",
                    count=1,
                    detail=detail,
                    detector="registry_freshness_master",
                    status="open",
                    count_kind="occurrence")
        try:
            c.commit()
        except Exception:
            pass
    except Exception as e:
        logger.debug("[regfresh] escalate skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        try:
            checks = fn(c)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = bool(decided) and all(ch["pass"] for ch in decided)
        lanes.append({"lane": key, "label": label, "pass": lane_pass,
                      "actuator": actuator, "checks": checks,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "pure-DB diagnostic (no self-requests); a RED lane now ESCALATES "
                "one brain_finding (registry_freshness_red) — the actual push stays "
                "owner-gated (REGISTRY_SUBMIT_ENABLED + daily crawler auto-fix).",
    }
    if c is not None:
        try:
            _ensure_snapshots(c)
            with c.cursor() as cur:
                cur.execute("INSERT INTO registry_freshness_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s, %s, %s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
        except Exception as e:
            logger.debug("[regfresh] snapshot insert failed: %s", e)
        _escalate_red(c, lanes)   # DETECT -> ESCALATE (r-actuate 2026-07-17)
        try:
            c.close()
        except Exception:
            pass
    return payload


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes (disabled → 404, NEVER 5xx: a 5xx trips the CF failover breaker) ──

@registry_freshness_master_shell_bp.route("/api/v1/admin/registry-freshness/master-tick",
                                          methods=["GET", "POST"])
def registry_freshness_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@registry_freshness_master_shell_bp.route("/admin/registry-freshness", methods=["GET"])
@registry_freshness_master_shell_bp.route("/api/v1/admin/registry-freshness", methods=["GET"])
def registry_freshness_dashboard():
    if _disabled():
        return Response("registry-freshness disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Registry Freshness Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Registry Freshness Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>07-06 · read-only DIAGNOSTIC "
        f"(pure-DB, no self-requests; names an actuator per lane, fires nothing) · "
        f"30s tick cache · auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/registry-freshness/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
