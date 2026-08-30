"""
ai_surface_sentinel.py — AI-Surface Freshness Sentinel (Phase 1: read-only audit).
==================================================================================

Keeps every AI-agent-facing surface consistent with ai_surface_canon (the one
source of truth). This session proved the need: the manifest chain disagreed
with itself (v2.1.22 / 2.3.3 / 2.1.0), AGENTS.md said both "24 tools" and "48
tools", /connect carried an inflated five-figure facility claim, robots.txt
served stale.

Phase 1 = the read-only AUDIT (zero write-risk): fetch each surface cache-
bypassed, diff against canon, return a scorecard. AUTO-FIX is scaffolded but
gated OFF by default (observe-first, matching the brain auto-merge-OFF pattern) —
flip AI_SURFACE_SENTINEL_AUTOFIX=1 later once the scorecard is trusted.

  GET  /api/v1/admin/ai-surface/audit    → drift scorecard (no writes)
  GET  /api/v1/admin/ai-surface/canon    → the resolved canon (live numbers)
  POST /api/v1/admin/ai-surface/refresh  → autofix (gated; today just audits)

Kill: AI_SURFACE_SENTINEL_ENABLED=0 (cron; ON by default since 2026-08-30 —
see sentinel_cron_enabled below), AI_SURFACE_SENTINEL_AUTOFIX=1 (writes; still
opt-in, untouched).
"""
from __future__ import annotations

import json
import os
import random
import urllib.request

from flask import Blueprint, jsonify, request
from util.json_column import json_for_column


# ★★2026-08-30 — THE AUDIT NOW RUNS BY DEFAULT.
#
# This flag defaulted to OFF, and that default is why every agent-facing
# surface drifted. The rest of the chain was complete and working:
# ai_surface_canon is the source, canon_text() interpolates it, and
# white_glove_propagation pushes it to the registries and HAS been running
# (11 real runs 07-19..08-04; its cadence bug was fixed at the source by
# #2283). Only the half that CHECKS the result was gated off — so canon went
# out, the surfaces drifted away from it afterwards, and nothing noticed.
#
# What that default cost, measured 2026-08-30: one live why_dchub response
# served 18,500+ (edges), 19,700+ (pitch) and 21,900+ (provenance_note)
# facilities for the same claim, against 19,500+ in the MCP server's own
# instructions. On the frontend, nine distinct facility magnitudes and seven
# tool counts across served surfaces, and the six-document agent discovery
# ring (agent-card.json, skill.json, openapi.json, the MCP server card, the
# OpenAI tool registry, capabilities.html) disagreeing with itself on three of
# the four claims it publishes. An outside AI auditing DC Hub from the public
# side found it unaided and named it the platform's main remaining weakness
# for agents: a human notices the contradiction and picks one, an agent walks
# the chain and gets a different magnitude at each hop with no basis to choose.
#
# ai-surface-partner-sync.yml already named the cause exactly — "It is a
# disabled flag, not a missing cron" — on 2026-08-06, and the white-glove lane
# has reported it critical ever since ("NEVER — no ai_surface_audits row
# exists"). Both were correct, and neither could turn it on. More reporting
# was never the missing piece.
#
# SAFE TO DEFAULT ON. Phase 1 is the read-only audit; this module's own
# docstring calls it "zero write-risk". It fetches each surface cache-bypassed,
# diffs against canon, and persists one ai_surface_audits row. Regenerating a
# surface FROM canon is a separate lane behind a separate flag
# (AI_SURFACE_SENTINEL_AUTOFIX), which stays opt-in and is not touched here.
#
# ROLLBACK is one env var: AI_SURFACE_SENTINEL_ENABLED=0. Kept as a real kill
# switch rather than deleted, so the cron can be stopped without a deploy.
def sentinel_cron_enabled() -> bool:
    """True unless AI_SURFACE_SENTINEL_ENABLED is explicitly set falsy.

    Read by BOTH gate sites — the /refresh endpoint in this module and the
    scheduler slot in crawler_scheduler.py — so the two can never disagree
    about whether the sentinel is on. They were previously two independent
    inline env reads that happened to match.
    """
    raw = str(os.environ.get("AI_SURFACE_SENTINEL_ENABLED", "")).strip().lower()
    if raw == "":
        return True                      # ← the change: absent now means ON
    return raw in ("1", "true", "yes", "on")


ai_surface_sentinel_bp = Blueprint("ai_surface_sentinel", __name__)

_PUBLIC = "https://dchub.cloud"

# (key, url, kind) — the surfaces to audit each run.
_SURFACES = [
    ("llms_txt",             f"{_PUBLIC}/llms.txt",                              "text"),
    ("llms_full",            f"{_PUBLIC}/llms-full.txt",                         "text"),
    ("agents_md",            f"{_PUBLIC}/AGENTS.md",                             "text"),
    # audited as TEXT, not json: this manifest is proxied by the off-repo zone
    # worker whose version runs one patch AHEAD of PINNED by design (daily
    # auto-publish), so the json version-check would false-RED. Text mode still
    # runs the stale-marker + free-tier scans — which is what we want here (it's
    # where the owner-gated "3 calls/day" + "4,000+ deals" zone-worker prose live).
    ("mcp_json",             f"{_PUBLIC}/.well-known/mcp.json",                  "text"),
    ("mcp_server_json",      f"{_PUBLIC}/.well-known/mcp-server.json",           "json"),
    ("server_card",          f"{_PUBLIC}/.well-known/mcp/server-card.json",      "json"),
    ("openapi",              f"{_PUBLIC}/openapi.json",                          "json"),
    ("chatgpt_instructions", f"{_PUBLIC}/integrations/chatgpt/instructions.txt", "text"),
    ("grok_config",          f"{_PUBLIC}/integrations/grok/mcp-config.json",     "text"),
    ("robots",               f"{_PUBLIC}/robots.txt",                            "text"),
    ("connect",              f"{_PUBLIC}/connect",                               "text"),
    ("ai_page",              f"{_PUBLIC}/ai",                                    "text"),
]


def _admin_ok() -> bool:
    import hmac
    ak = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    ik = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    ga = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    gi = (request.headers.get("X-Internal-Key") or "").strip()
    if ak and ga and hmac.compare_digest(ga, ak):
        return True
    if ik and gi and hmac.compare_digest(gi, ik):
        return True
    return False


def _fetch(url, timeout=15):
    sep = "&" if "?" in url else "?"
    u = f"{url}{sep}cb={str(random.random())[2:9]}"
    req = urllib.request.Request(u, method="GET")
    req.add_header("Cache-Control", "no-cache")
    req.add_header("User-Agent", "dchub-ai-surface-sentinel/1")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read().decode("utf-8", "replace")


_HIGH = {"10,706", "10706", "50,000+", "2.1.22", "2.3.3", "2.1.0",
         "24 tools", "48 tools", "49 tools", "51 tools", "53 tools",
         "58 tools", "60 tools", "72 tools", "81 tools"}


def _audit_surface(key, url, kind, canon):
    drifts = []
    try:
        code, body = _fetch(url)
    except Exception as e:
        return {"surface": key, "live_url": url, "verdict": "unreachable",
                "drifts": [{"field": "fetch", "live": f"{type(e).__name__}: {str(e)[:70]}",
                            "expected": "200", "severity": "medium"}]}
    if code != 200:
        return {"surface": key, "live_url": url, "verdict": "unreachable",
                "drifts": [{"field": "http", "live": str(code), "expected": "200",
                            "severity": "medium"}]}

    def add(field, live, expected, sev):
        drifts.append({"field": field, "live": live, "expected": expected, "severity": sev})

    # 1) known-stale markers present in the served body
    for m in canon.get("stale_markers", []):
        if m in body:
            add("stale_value", m, "update-from-canon", "high" if m in _HIGH else "medium")
    # 2) fake tool names
    for f in canon.get("fake_tool_denylist", []):
        if f in body:
            add("fake_tool_name", f, "real tool name", "high")
    # 3) manifests: precise version + tool-count check
    if kind == "json":
        try:
            d = json.loads(body)
            v = str(d.get("version") or (d.get("info") or {}).get("version") or "")
            if v and v != canon["version"]:
                add("version", v, canon["version"], "high")
            tools = d.get("tools")
            ok_counts = {canon.get("tools_live"), canon.get("tools_advertised")}
            if isinstance(tools, list) and len(tools) not in ok_counts:
                add("tools[]_count", str(len(tools)), str(canon.get("tools_live") or canon.get("tools_advertised")), "high")
        except Exception:
            add("json_parse", "invalid/HTML", "valid JSON", "high")
    # 4) robots.txt must welcome the required crawlers
    if key == "robots":
        for cw in canon.get("crawlers_required", []):
            if cw not in body:
                add("crawler_missing", "absent", cw, "medium")
    # 5) free-tier overstatement on human pages
    if key in ("connect", "ai_page") and "100 calls/day" in body:
        add("free_tier", "100 calls/day", "10 calls/day", "medium")
    # 5b) free-tier UNDERSTATEMENT on any served surface (2026-07-17). The
    # anonymous keyless limit is the canon's free_tier_calls_per_day (10/day); a
    # served "3 calls/day" is the old MCP_FREE_DAILY_LIMIT=3 — stale prose that
    # now survives only in the off-repo zone worker (dchubapiproxy: mcp.json
    # pricing.anonymous). That surface is owner-gated (CF dashboard paste, no
    # API) so this is DETECT-only — the honest ceiling — but at least the drift
    # now shows up on the audit instead of silently understating the free tier.
    ftc = canon.get("free_tier_calls_per_day", 10)
    if "3 calls/day" in body:
        add("free_tier_anon", "3 calls/day", f"{ftc} calls/day", "medium")
    # 5c) PAID tiers quoted per DAY (monthly-quota phase 2, 2026-08-06).
    # Paid ceilings are enforced per MONTH now (monthly_quota.py); their
    # per-day caps were never enforced on the /mcp path at all. A served
    # "200 calls/day" therefore quotes a limit that does not exist — and
    # it is 1/30th of what the customer actually bought.
    #
    # ★ Deliberately does NOT touch free/identified copy: those tiers are
    # still gated per day and "10 calls/day" is the honest number there.
    # Rewriting free copy to a monthly figure would advertise a ceiling
    # the free gate does not grant, so this check is scoped to the paid
    # per-day literals and nothing else.
    for _tier, _daily in (("starter", 200), ("developer", 500), ("pro", 2000)):
        _monthly = canon.get(f"{_tier}_calls_per_month")
        if not _monthly:
            continue
        for _lit in (f"{_daily} calls/day", f"{_daily:,} calls/day"):
            if _lit in body:
                add(f"{_tier}_period", _lit, f"{_monthly:,} calls/month", "medium")
                break

    if not drifts:
        verdict = "clean"
    elif any(x["severity"] == "high" for x in drifts):
        verdict = "major-drift"
    else:
        verdict = "minor-drift"
    return {"surface": key, "live_url": url, "verdict": verdict, "drifts": drifts}


def run_audit() -> dict:
    from ai_surface_canon import resolve_canon
    canon = resolve_canon()
    results = [_audit_surface(k, u, kind, canon) for (k, u, kind) in _SURFACES]
    counts = {"clean": 0, "minor-drift": 0, "major-drift": 0, "unreachable": 0}
    total_drifts = 0
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        total_drifts += len(r["drifts"])
    return {
        "canon": {k: canon.get(k) for k in
                  ("version", "facilities_live", "markets_live", "tools_live", "deals_live")},
        "summary": counts,
        "total_drifts": total_drifts,
        "surfaces": sorted(results, key=lambda r: {"major-drift": 0, "minor-drift": 1,
                                                   "unreachable": 2, "clean": 3}[r["verdict"]]),
    }


# ── Persistence (2026-08-06) ─────────────────────────────────────────
# ★WHY THE AUDIT NOW LEAVES A TRACE. Phase 1 returned the scorecard over HTTP
# and stored NOTHING, so the verdict existed only in whoever's terminal ran it.
# Nothing was scheduled either, so in practice nobody ran it — and no other
# surface could ask "when did our published numbers last agree?" because there
# was no row to read. The white-glove AI-surface lane needs that row: it must be
# able to distinguish NEVER RAN from RAN AND CLEAN, and a scorecard that
# evaporates makes those two look identical.
#
# Writes are BEST-EFFORT and never raise. An audit that cannot persist still
# returns its scorecard — losing the row must not lose the answer.
def _ensure_audits_table(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ai_surface_audits ("
        " id BIGSERIAL PRIMARY KEY,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " surfaces_checked INT, clean INT, minor_drift INT, major_drift INT,"
        " unreachable INT, total_drifts INT, payload JSONB)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ai_surface_audits_recent_idx"
        " ON ai_surface_audits (created_at DESC)")


def _audit_db_conn():
    """Own connection, autocommit ON.

    ★NOT `with psycopg2.connect(...)` — that context manager is a TRANSACTION
    manager, so one failing statement poisons every later one on the connection
    (the InFailedSqlTransaction bug fixed in brain_capability_radar, #2071).
    """
    try:
        import psycopg2
        url = ((os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def persist_audit(result: dict) -> bool:
    """Record one audit. Returns True iff a row landed. Never raises."""
    try:
        from routes._swallowed_writes import note_swallowed_write
    except Exception:
        def note_swallowed_write(*_a, **_k):
            return None

    conn = _audit_db_conn()
    if conn is None:
        note_swallowed_write("ai_surface_audits", "ai_surface_sentinel.persist_audit")
        return False
    try:
        s = (result or {}).get("summary") or {}
        surfaces = (result or {}).get("surfaces") or []
        with conn.cursor() as cur:
            _ensure_audits_table(cur)
            # ★ONE triple-quoted string, and ON CONFLICT DO NOTHING is real, not
            # decoration. Two things force this shape:
            #  1. regression_lint's insert-no-on-conflict rule scans with
            #     `INSERT\s+INTO\s+(\w+)[^;"']*` — the window STOPS at the first
            #     quote, so an ON CONFLICT split across adjacent string
            #     fragments is invisible to it. That blindness is why
            #     brain_llm_usage and slow_requests sit on WHITELIST_TABLES
            #     despite carrying the clause. A single triple-quoted literal
            #     keeps the clause inside the window, so this table needs no
            #     whitelist entry — no guard is touched to land this code.
            #  2. The clause is a no-op TODAY (the only key is a server-side
            #     BIGSERIAL, so nothing can collide) but it is not pointless:
            #     if a natural key is added later — say UNIQUE(created_at::date)
            #     to force one audit per day — a bare INSERT would start
            #     crashing the nightly job, and a crashed audit is what makes
            #     the white-glove lane read "never ran".
            cur.execute("""
                INSERT INTO ai_surface_audits (surfaces_checked, clean,
                    minor_drift, major_drift, unreachable, total_drifts, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """,
                (len(surfaces), int(s.get("clean") or 0),
                 int(s.get("minor-drift") or 0), int(s.get("major-drift") or 0),
                 int(s.get("unreachable") or 0),
                 int((result or {}).get("total_drifts") or 0),
                 json_for_column(result or {}, 400000)))
        return True
    except Exception:
        note_swallowed_write("ai_surface_audits", "ai_surface_sentinel.persist_audit")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ai_surface_sentinel_bp.route("/api/v1/admin/ai-surface/audit", methods=["GET"])
def ai_surface_audit():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    result = run_audit()
    # ?persist=0 for an ad-hoc look that must not enter the record.
    want = (request.args.get("persist") or "1").strip().lower()
    recorded = persist_audit(result) if want not in ("0", "false", "no") else False
    # `recorded` is REPORTED, not swallowed: a scheduled run that audits fine but
    # cannot write is the exact shape of weekly-shadow-audit's two green weeks.
    return jsonify(ok=True, recorded=recorded, **result), 200


@ai_surface_sentinel_bp.route("/api/v1/admin/ai-surface/canon", methods=["GET"])
def ai_surface_canon_dump():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    from ai_surface_canon import resolve_canon
    return jsonify(ok=True, canon=resolve_canon()), 200


# Frontend-owned surfaces the drift PR writer may draft fixes for, mapped to
# their file paths at the ROOT of azmartone67/dchub-frontend (verified layout:
# llms.txt / llms-full.txt / AGENTS.md / ai.json all live at repo root).
_FRONTEND_SURFACE_PATHS = {
    "llms_txt": "llms.txt",
    "llms_full": "llms-full.txt",
    "agents_md": "AGENTS.md",
}


def _count_fix_replacement(marker, canon):
    """Canonical replacement for a SIMPLE count-string stale marker, or None.
    Deliberately narrow: only unambiguous count strings get a mechanical fix
    (versions and bare market numbers like '232 ' are too context-dependent
    for a string swap — those stay findings for a human)."""
    if not marker:
        return None
    pub = canon.get("public") or {}
    tools = str(canon.get("tools_advertised") or "")
    fixes = {
        "50,000+": pub.get("facilities"),
        "100 calls/day": f"{canon.get('free_tier_calls_per_day', 10)} calls/day",
        "24 tools": f"{tools} tools" if tools else None,
        "48 tools": f"{tools} tools" if tools else None,
        "49 tools": f"{tools} tools" if tools else None,
        "51 tools": f"{tools} tools" if tools else None,
        "53 tools": f"{tools} tools" if tools else None,
        "58 tools": f"{tools} tools" if tools else None,
        "60 tools": f"{tools} tools" if tools else None,
        "72 tools": f"{tools} tools" if tools else None,
        "81 tools": f"{tools} tools" if tools else None,
    }
    return fixes.get(marker)


def _draft_drift_fix_prs(audit):
    """For drifts that are simple count-string mismatches on frontend-owned
    surfaces, ALSO draft a mechanical fix PR via routes.drift_pr_writer.
    ON RAILS: the writer is DRY_RUN unless DCHUB_DRIFT_PR=1 (default OFF —
    it only reports what it WOULD do), draft-PR-only, fenced + allowlisted.
    Fail-soft: never raises, never blocks the findings write."""
    try:
        from ai_surface_canon import PINNED
        from routes.drift_pr_writer import MAX_EDITS_PER_PR, open_drift_fix_pr
    except Exception as e:
        return {"attempted": False, "error": f"import: {str(e)[:80]}"}
    edits, seen = [], set()
    for s in audit.get("surfaces", []):
        path = _FRONTEND_SURFACE_PATHS.get(s.get("surface"))
        if not path:
            continue
        for d in s.get("drifts", []):
            if d.get("field") != "stale_value":
                continue
            replace = _count_fix_replacement(d.get("live"), PINNED)
            if not replace or (path, d["live"]) in seen:
                continue
            seen.add((path, d["live"]))
            edits.append({"path": path, "find": d["live"], "replace": replace})
    if not edits:
        return {"attempted": False,
                "reason": "no simple count-string drift on frontend surfaces"}
    try:
        return open_drift_fix_pr(
            "azmartone67/dchub-frontend", edits[:MAX_EDITS_PER_PR],
            rationale=("ai_surface_sentinel audit found stale count strings "
                       "on frontend-owned AI surfaces; canonical values from "
                       "ai_surface_canon.PINNED."))
    except Exception as e:
        return {"attempted": True, "error": str(e)[:120]}


def _write_findings(audit):
    """Upsert each drift into brain_findings (dedup on issue+url) so drift is
    tracked + actionable in the brain workflow. SAFE: findings are informational
    — no surface writes. Uses the canonical writer (handles the UNIQUE(issue,url)
    schema trap; a hand-rolled INSERT with the wrong columns fails silently)."""
    try:
        from main import get_pg_connection, return_pg_connection
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        return {"written": 0, "error": f"import: {str(e)[:80]}"}
    conn = None
    written = 0
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            for s in audit.get("surfaces", []):
                for d in s.get("drifts", []):
                    issue = f"ai_surface_drift:{s['surface']}:{d['field']}"
                    detail = (f"{s['surface']} {d['field']}: live={d['live']!r} "
                              f"expected={d['expected']!r} sev={d['severity']} "
                              f"({s['live_url']})")
                    try:
                        upsert_brain_finding(cur, issue=issue, url=s["live_url"],
                                             detail=detail, detector="ai_surface_sentinel")
                        written += 1
                    except Exception:
                        pass
        conn.commit()
    except Exception as e:
        return {"written": written, "error": str(e)[:120]}
    finally:
        if conn is not None:
            try:
                return_pg_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass
    # Simple count-string drift on frontend surfaces ALSO gets a mechanical
    # draft-PR attempt (DRY_RUN unless DCHUB_DRIFT_PR=1; fail-soft).
    return {"written": written, "drift_pr": _draft_drift_fix_prs(audit)}


@ai_surface_sentinel_bp.route("/api/v1/admin/ai-surface/refresh", methods=["POST"])
def ai_surface_refresh():
    """Cron-driven acting endpoint. Runs the audit, and — when the sentinel is
    ENABLED (or ?force) — writes each drift to brain_findings so the brain
    tracks + can act on it. AUTO-FIX (regenerating surfaces from canon) is a
    separate, still-gated lane (Phase-2b); findings-only is the safe default."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    enabled = sentinel_cron_enabled()
    autofix_on = str(os.environ.get("AI_SURFACE_SENTINEL_AUTOFIX", "")).lower() in ("1", "true", "yes")
    audit = run_audit()
    if enabled or body.get("force"):
        findings = _write_findings(audit)
    else:
        findings = {"written": 0, "skipped": "AI_SURFACE_SENTINEL_ENABLED=0"}
    return jsonify(
        ok=True,
        enabled=enabled,
        autofix=("enabled_pending_impl" if autofix_on else "disabled — findings only"),
        findings=findings,
        **audit,
    ), 200
