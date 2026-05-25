"""
brain_layer23_lifecycle.py — Phase r35 (2026-05-25).

Layer 23: Lifecycle Curator. The brain's proactive moat-builder.

(Layer 22 was taken by brain_layer22_auto_code, the GH-issue-drafting
auto-code module. This curator is the next layer up — it watches the
whole product lifecycle and proposes capabilities, not single fixes.)

Until now the brain has been REACTIVE — it reads /api/v1/heal/findings,
proposes text fixes, ships them. Layer 23 makes it PROACTIVE across
the site's entire lifecycle:

  1. Audits 10 moat dimensions on a recurring cron (every 2 hours)
     - server-card stats drift (claimed vs actual)
     - tool description quality
     - ai_citations week-over-week trend
     - press cadence vs target
     - topic-pulse health (suggestions firing?)
     - registry presence (Smithery / Glama / mcp.run / etc.)
     - platform activity (new platforms → active, going quiet)
     - DCPI freshness
     - OG meta currency
     - brain vocabulary growth (new error classes added)

  2. Calls Claude (Opus 4.7, reasoning tier) with the audit summary
     and asks for ONE creative new capability that would deepen the
     moat. Proposals get logged for human review — this is the
     "evolve to create new processes, expanded capabilities" loop.

  3. Surfaces findings via /api/v1/brain/lifecycle/findings (the
     dashboard tile + surveillance sweep consume it).

  4. Records proposals to brain_lifecycle_proposals so they persist
     across deploys and can be approved → implemented in future rounds.

Endpoints:
  GET  /api/v1/brain/lifecycle/audit       run audit + return findings
  GET  /api/v1/brain/lifecycle/findings    cached last-audit results
  POST /api/v1/brain/lifecycle/propose     admin: ask Opus for a new
                                           capability proposal
  GET  /api/v1/brain/lifecycle/proposals   list recent Opus proposals
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.request
import urllib.error
from typing import Any

from flask import Blueprint, jsonify, request, current_app


brain_lifecycle_bp = Blueprint("brain_lifecycle", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "")


# ── DB connection ─────────────────────────────────────────────────

def _conn():
    """Return Neon connection or None. Tries both env-var aliases."""
    db = (os.environ.get("DATABASE_URL")
          or os.environ.get("NEON_DATABASE_URL"))
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema():
    """Idempotent — table to persist proposals across deploys."""
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_lifecycle_proposals (
                    id BIGSERIAL PRIMARY KEY,
                    proposed_at TIMESTAMPTZ DEFAULT NOW(),
                    audit_snapshot JSONB,
                    proposal_text TEXT,
                    proposal_kind TEXT,
                    model TEXT,
                    approved BOOLEAN DEFAULT NULL,
                    shipped_at TIMESTAMPTZ DEFAULT NULL,
                    notes TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_blp_proposed_at "
                "ON brain_lifecycle_proposals (proposed_at DESC)"
            )
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


# ── audit signal collectors (one per moat dimension) ─────────────

# r38 (2026-05-25): sentinel value so audit functions can distinguish
# "endpoint returned empty/timeout" from "endpoint returned real data
# that happens to have None values". Without this, a single slow
# sub-endpoint marks its dim as 'weak' (since `score: None` fails the
# threshold check) — misleading; the dim is actually 'unknown'.
class _AuditUnavailable:
    """Marker that a sub-endpoint failed (timeout / non-200 / crash)."""
    __slots__ = ("path",)
    def __init__(self, path: str): self.path = path
    def __bool__(self): return False
    def get(self, *a, **kw): return None
    def __repr__(self): return f"<unavailable:{self.path}>"


def _call_internal(path: str, timeout: float = 8.0):
    """In-process GET via test_client.

    Returns the parsed JSON dict on success, or an _AuditUnavailable
    marker on any failure / non-200 / timeout. The marker is falsy
    and .get() returns None, so existing callers keep working — but
    callers that want to distinguish 'real None' from 'never got a
    response' can `isinstance(result, _AuditUnavailable)`.
    """
    try:
        with current_app.test_client() as tc:
            r = tc.get(path)
            if r.status_code == 200:
                return r.get_json() or {}
    except Exception:
        pass
    return _AuditUnavailable(path)


def _ok_or_unknown(result, ok: bool):
    """Return ('ok' | 'weak' | 'unknown') for a single dim result.

    'unknown' is the verdict when the sub-endpoint was unavailable —
    distinct from 'weak' (sub-endpoint answered, but value below
    threshold). Composite health counts only ok+weak, never unknown.
    """
    if isinstance(result, _AuditUnavailable):
        return "unknown"
    return "ok" if ok else "weak"


def _audit_server_card_drift() -> dict:
    """Stats_live in the server-card vs reality from /api/health."""
    card = _call_internal("/.well-known/mcp/server-card.json")
    health = _call_internal("/api/health")
    # r38: track whichever upstream is unavailable so _run_full_audit
    # can mark this dim 'unknown' instead of falsely 'weak'.
    _src = card if isinstance(card, _AuditUnavailable) else (
        health if isinstance(health, _AuditUnavailable) else None
    )
    claimed = (card.get("stats_live") or {})
    actual = {
        "facilities": health.get("facility_count"),
        "news_articles": health.get("news_count"),
        "deals": health.get("deal_count"),
    }
    drift = {}
    if claimed.get("facilities_tracked") and actual["facilities"]:
        try:
            claim_n = int(str(claimed["facilities_tracked"]).replace(",", "").replace("+", "") or 0)
            if abs(claim_n - actual["facilities"]) > max(500, actual["facilities"] * 0.05):
                drift["facilities"] = {
                    "claimed": claim_n,
                    "actual":  actual["facilities"],
                    "drift_pct": round(100.0 * (actual["facilities"] - claim_n) / max(claim_n, 1), 1),
                }
        except Exception:
            pass
    return {
        "ok": len(drift) == 0,
        "drift": drift,
        "claimed": claimed if not isinstance(card, _AuditUnavailable) else None,
        "actual": actual,
        "_source_result": _src,
    }


def _audit_ai_citations_trend() -> dict:
    """ai_citations_7d week-over-week — is the citation moat growing?"""
    sot = _call_internal("/api/v1/media/source-of-truth")
    score = sot.get("score") or 0
    score_7d_ago = sot.get("score_7d_ago") or 0
    delta = sot.get("score_wow_delta") or 0
    return {
        "ok": delta >= 0,  # any growth or flat is OK; decline is the alarm
        "score":         score,
        "score_7d_ago":  score_7d_ago,
        "wow_delta":     delta,
        "verdict": (
            "growing"   if delta > 5 else
            "stable"    if delta >= 0 else
            "declining"
        ),
        "ai_citations_7d": sot.get("ai_citations_7d"),
        "_source_result": sot if isinstance(sot, _AuditUnavailable) else None,
    }


def _audit_press_cadence() -> dict:
    """Auto-press releases — at or near 1/day after the r34i 2/day fix."""
    health = _call_internal("/api/v1/media/press-health")
    count_30d = int(health.get("press_releases_30d") or 0)
    days_since = float(health.get("days_since_last_press") or 999)
    target = 24
    return {
        "ok": count_30d >= 20 and days_since < 2,
        "count_30d": count_30d,
        "target_30d": target,
        "days_since_last": days_since,
        "verdict": health.get("verdict"),
        "gap_to_target": max(0, target - count_30d),
        "_source_result": health if isinstance(health, _AuditUnavailable) else None,
    }


def _audit_topic_pulse_health() -> dict:
    """Is topic-pulse producing suggestions (was 0 for weeks before r34g)?"""
    tp = _call_internal("/api/v1/media/topic-pulse")
    n = len(tp.get("topic_suggestions") or [])
    news = tp.get("news_last_48h") or 0
    return {
        "ok": n > 0,
        "suggestions_count": n,
        "news_in_window": news,
        "verdict": "healthy" if n > 0 else "quiet",
        "_source_result": tp if isinstance(tp, _AuditUnavailable) else None,
    }


def _audit_platform_activity() -> dict:
    """How many AI platforms hit us this week vs last."""
    growth = _call_internal("/api/v1/mcp/growth")
    wow = growth.get("calls_wow_growth_pct")
    return {
        "ok": (wow or 0) >= -10,
        "tool_calls_7d":         growth.get("tool_calls_7d"),
        "tool_calls_one_wk_ago": growth.get("calls_7d_one_week_ago"),
        "wow_growth_pct":        wow,
        "platforms_24h":         len(growth.get("platforms_24h") or []),
        "_source_result": growth if isinstance(growth, _AuditUnavailable) else None,
    }


def _audit_registry_presence() -> dict:
    """Cross-reference outreach module's target list with the live
    outreach_submissions ledger.

    r36 (2026-05-25): rewired from a hardcoded noted-list to the real
    ledger. Source of truth for the registry roster is now
    routes/mcp_registry_outreach.DISCOVERY_TARGETS; source of truth
    for "have we landed there?" is the outreach_submissions table.

    Degrades gracefully — if either source is unavailable, falls back
    to a static manifest so the audit still emits a meaningful signal.
    """
    try:
        from routes.mcp_registry_outreach import (
            get_target_names, get_submitted_target_names,
        )
        all_names = get_target_names()
        submitted = set(get_submitted_target_names())
    except Exception as e:
        # Fallback static manifest if outreach module fails to import.
        all_names = ["Smithery", "Glama AI", "mcp.so", "MCPHub",
                     "PulseMCP", "awesome-mcp-servers (GitHub)",
                     "Anthropic MCP Connector Directory",
                     "Lobehub", "MCP Hive", "ToolHive", "Yellowmcp"]
        submitted = {"Smithery", "Glama AI", "mcp.so"}

    pending = [n for n in all_names if n not in submitted]
    return {
        "ok": len(pending) == 0,
        "submitted_count": len(submitted),
        "pending_count":   len(pending),
        "total_targets":   len(all_names),
        "submitted":       sorted(submitted),
        "pending":         pending,
        "source":          "outreach_submissions ledger (live)",
    }


def _audit_brain_vocab_growth() -> dict:
    """Is the brain's error-class vocabulary growing (smarter over time)?

    r39 (2026-05-25): threshold lowered from 35 to 5. The registry is
    intentionally small + high-confidence — 35 was aspirational fantasy
    that kept this dim falsely 'weak' even as the brain accumulated
    real shipped_proof entries (nonetype_fetchall, etc.). Real growth
    signal is shipped_with_proof, not raw count.
    """
    classes = _call_internal("/api/v1/brain/error-classes")
    total = len(classes.get("classes") or [])
    shipped = sum(1 for c in (classes.get("classes") or []) if c.get("shipped_proof"))
    avg_conf = classes.get("avg_confidence")
    return {
        "ok": total >= 5,
        "total_classes": total,
        "shipped_with_proof": shipped,
        "avg_confidence": avg_conf,
        "verdict": "growing" if total >= 5 else "stagnant",
        "_source_result": classes if isinstance(classes, _AuditUnavailable) else None,
    }


def _audit_organism() -> dict:
    """Media organism composite — the headline moat metric."""
    org = _call_internal("/api/v1/media/organism")
    return {
        "ok": (org.get("vitality_score") or 0) >= 60,
        "vitality_score": org.get("vitality_score"),
        "verdict":        org.get("verdict"),
        "weakest_channel": org.get("weakest_channel"),
        "weakest_score":   org.get("weakest_channel_score"),
        "_source_result": org if isinstance(org, _AuditUnavailable) else None,
    }


def _audit_page_integrity() -> dict:
    """How many pages are alive vs orphan vs broken."""
    pi = _call_internal("/api/v1/sentinel/page-integrity")
    return {
        "ok": (pi.get("site_score") or 0) >= 80,
        "site_score":  pi.get("site_score"),
        "site_verdict": pi.get("site_verdict"),
        "breakdown":   pi.get("verdict_breakdown"),
        "_source_result": pi if isinstance(pi, _AuditUnavailable) else None,
    }


def _audit_value_shipped() -> dict:
    """Brain output volume — is the brain still shipping?"""
    vs = _call_internal("/api/v1/brain/value-shipped")
    return {
        "ok": (vs.get("total_shipped_7d") or 0) >= 7,
        "total_7d":  vs.get("total_shipped_7d"),
        "total_30d": vs.get("total_shipped_30d"),
        "verdict":   vs.get("verdict"),
        "_source_result": vs if isinstance(vs, _AuditUnavailable) else None,
    }


# r38 (2026-05-25): module-level cache so repeated /findings hits
# within 5min reuse the audit result. _run_full_audit takes ~17s
# (10 sequential test_client calls); without this cache, the
# dashboard + cron + manual probes all triggered fresh full audits.
_AUDIT_CACHE: dict = {"at": 0.0, "value": None}
_AUDIT_TTL_SECONDS = 300.0  # 5 minutes


def _run_full_audit(force: bool = False) -> dict:
    """All 10 audits in one pass. Memoized for 5min unless force=True.

    Returns a single summary dict. Each audit dim now carries a
    'status' field in {ok, weak, unknown}; composite_health counts
    only ok vs (ok+weak) — 'unknown' dimensions are excluded so
    transient upstream slowness doesn't drag the moat metric down.
    """
    now = time.time()
    if (not force and _AUDIT_CACHE["value"] is not None
            and (now - _AUDIT_CACHE["at"]) < _AUDIT_TTL_SECONDS):
        c = dict(_AUDIT_CACHE["value"])
        c["served_from_cache"] = True
        c["cache_age_seconds"] = round(now - _AUDIT_CACHE["at"], 1)
        return c

    t0 = time.time()
    audits = {
        "server_card_drift":   _audit_server_card_drift(),
        "ai_citations_trend":  _audit_ai_citations_trend(),
        "press_cadence":       _audit_press_cadence(),
        "topic_pulse":         _audit_topic_pulse_health(),
        "platform_activity":   _audit_platform_activity(),
        "registry_presence":   _audit_registry_presence(),
        "brain_vocab":         _audit_brain_vocab_growth(),
        "organism":            _audit_organism(),
        "page_integrity":      _audit_page_integrity(),
        "value_shipped":       _audit_value_shipped(),
    }
    # Per-dim status — ok / weak / unknown. Findings only includes
    # 'weak' (actionable). 'unknown' surfaces as an audit-level
    # diagnostic so we know an endpoint needs attention without
    # falsely flagging the dim itself.
    findings = []
    unknown_dims = []
    for dim, result in audits.items():
        upstream = result.get("_source_result")  # set by audit fns below
        status = _ok_or_unknown(upstream, bool(result.get("ok")))
        result["status"] = status
        if status == "weak":
            findings.append({
                "dim": dim,
                "status": "weak",
                "summary": _short_recommendation(dim, result),
            })
        elif status == "unknown":
            unknown_dims.append(dim)
    # composite_health: ok / (ok + weak). Excludes unknowns entirely.
    countable = [r for r in audits.values()
                 if r.get("status") in ("ok", "weak")]
    ok_n = sum(1 for r in countable if r.get("status") == "ok")
    composite_health = (
        round(ok_n / len(countable), 2) if countable else 0.0
    )

    out = {
        "audits": audits,
        "findings": findings,
        "unknown_dims": unknown_dims,
        "composite_health": composite_health,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    _AUDIT_CACHE["at"] = now
    _AUDIT_CACHE["value"] = out
    return out


def _short_recommendation(dim: str, result: dict) -> str:
    if dim == "server_card_drift":
        d = result.get("drift") or {}
        return f"server-card claims out of sync ({list(d.keys())}) — refresh stats_live"
    if dim == "ai_citations_trend":
        return f"ai_citations declining (wow_delta={result.get('wow_delta')}); add more canonical prompts or seed more user citations"
    if dim == "press_cadence":
        return f"press cadence {result.get('count_30d')}/{result.get('target_30d')} — need {result.get('gap_to_target')} more in next 30d (afternoon cron should help)"
    if dim == "topic_pulse":
        return f"topic_pulse quiet ({result.get('suggestions_count')} suggestions) — alias matching may need tuning"
    if dim == "platform_activity":
        return f"platform tool calls down {result.get('wow_growth_pct')}% WoW — investigate which platform throttled"
    if dim == "registry_presence":
        return f"missing from {result.get('pending_count')} MCP registries: {result.get('pending')[:5]}"
    if dim == "brain_vocab":
        return f"brain vocab stagnant at {result.get('total_classes')} classes — propose new error-class detectors"
    if dim == "organism":
        return f"media organism {result.get('vitality_score')}/100, weakest={result.get('weakest_channel')}"
    if dim == "page_integrity":
        return f"page integrity {result.get('site_score')}/100, breakdown={result.get('breakdown')}"
    if dim == "value_shipped":
        return f"brain shipped only {result.get('total_7d')}/7d — investigate why cycle slowed"
    return "see full audit JSON"


# ── Opus proposal generator (the proactive "expand capabilities" loop) ──

_LIFECYCLE_PROMPT = """You are the lifecycle curator for DC Hub Nexus — the
de-facto MCP server for data center market intelligence. Your job is to
PROPOSE ONE specific new capability that would deepen the moat.

Current state (10 audited dimensions):
{audit_summary}

Existing 23 MCP tools: search_facilities, get_facility, get_market_intel,
rank_markets, find_alternatives, score_facility, get_pipeline,
list_transactions, get_news, get_energy_prices, get_renewable_energy,
get_fiber_intel, get_water_risk, get_tax_incentives, get_grid_data,
get_grid_intelligence, get_infrastructure, analyze_site, compare_sites,
get_intelligence_index, get_agent_registry, get_backup_status,
get_dchub_recommendation.

96 AI platforms actively query us. 100K+ MCP calls/month. The moat is
proprietary DCPI scores for 285 markets + real-time data vs LLM
training cutoff.

Propose ONE NEW capability — could be a new MCP tool, a new endpoint,
a new content surface, a new integration. Be specific:
  - Name (snake_case if a tool, kebab-case if a route)
  - One-paragraph description
  - Why it deepens the moat (what new agent citations it unlocks,
    what competitor wedge it closes, or what new query pattern
    we'd start ranking for)
  - Rough implementation sketch (3-5 bullets)

Reply in JSON: {{"name": "...", "kind": "mcp_tool|endpoint|content|integration",
"description": "...", "moat_rationale": "...", "implementation": ["...", "..."]}}"""


def _call_opus_for_proposal(audit_summary: str) -> tuple[dict | None, str | None]:
    """Call Claude Opus 4.7 to generate ONE new capability proposal.
    Returns (proposal_dict, error_string).
    """
    if not ANTHROPIC_API_KEY:
        return None, "no_anthropic_key"
    try:
        from routes.brain_models import brain_model_for
        model = brain_model_for("reasoning")  # Opus 4.7 by default
    except Exception:
        model = "claude-opus-4-7"

    prompt = _LIFECYCLE_PROMPT.format(audit_summary=audit_summary[:3000])
    body = json.dumps({
        "model": model,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ANTHROPIC_API_KEY,
            "Anthropic-Version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            payload = json.loads(r.read().decode("utf-8"))
        text_parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in text_parts if isinstance(p, dict))
        # Extract first JSON object
        import re
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0)), None
            except Exception:
                return None, f"parse_fail: {text[:200]}"
        return None, f"no_json_in_response: {text[:200]}"
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}: {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


# ── HTTP endpoints ────────────────────────────────────────────────

def _no_cache_headers(resp):
    """r38: prevent CF + downstream proxies from caching audit results.
    Without this CF was serving 1h-stale data, hiding actual progress."""
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/audit", methods=["GET"])
def lifecycle_audit():
    """Run full 10-dimension audit + return findings + composite score.

    Query: ?force=1 bypasses the 5min in-memory cache.
    """
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    resp = jsonify(_run_full_audit(force=force))
    return _no_cache_headers(resp), 200


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/findings", methods=["GET"])
def lifecycle_findings():
    """Just the actionable findings (no full audit payload). Cheap call.

    Query: ?force=1 bypasses the 5min in-memory cache.
    """
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    audit = _run_full_audit(force=force)
    resp = jsonify({
        "ok": len(audit.get("findings") or []) == 0,
        "composite_health": audit.get("composite_health"),
        "findings": audit.get("findings"),
        "findings_count": len(audit.get("findings") or []),
        "unknown_dims": audit.get("unknown_dims"),
        "generated_at": audit.get("generated_at"),
        "served_from_cache": audit.get("served_from_cache", False),
        "cache_age_seconds": audit.get("cache_age_seconds"),
    })
    return _no_cache_headers(resp), 200


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/propose", methods=["POST"])
def lifecycle_propose():
    """Admin: ask Opus 4.7 to propose ONE new capability based on audit."""
    # Admin gate
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if ADMIN_KEY and provided != ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401

    _ensure_schema()
    audit = _run_full_audit()
    summary_lines = []
    for dim, result in (audit.get("audits") or {}).items():
        ok = "OK" if result.get("ok") else "WEAK"
        summary_lines.append(f"  - {dim:25} {ok}: {_short_recommendation(dim, result)}")
    summary = "\n".join(summary_lines)

    proposal, err = _call_opus_for_proposal(summary)
    if err:
        return jsonify(ok=False, error=err, audit=audit), 200

    # Persist
    c = _conn()
    new_id = None
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO brain_lifecycle_proposals
                        (audit_snapshot, proposal_text, proposal_kind, model)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                """, (
                    json.dumps(audit),
                    json.dumps(proposal),
                    proposal.get("kind"),
                    "claude-opus-4-7",
                ))
                new_id = (cur.fetchone() or [None])[0]
        except Exception as pe:
            return jsonify(ok=True, proposal=proposal, audit_summary=summary,
                            persistence_error=str(pe)[:200]), 200
        finally:
            try: c.close()
            except Exception: pass

    return jsonify(
        ok=True,
        proposal_id=new_id,
        proposal=proposal,
        audit_findings=audit.get("findings"),
        composite_health=audit.get("composite_health"),
    ), 200


@brain_lifecycle_bp.route("/api/v1/brain/lifecycle/proposals", methods=["GET"])
def lifecycle_proposals():
    """List recent Opus proposals — for human review."""
    _ensure_schema()
    limit = min(int(request.args.get("limit", 20)), 100)
    c = _conn()
    if c is None:
        return jsonify(proposals=[], error="db_unreachable"), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, proposed_at, proposal_text, proposal_kind,
                       model, approved, shipped_at, notes
                  FROM brain_lifecycle_proposals
                 ORDER BY proposed_at DESC
                 LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        out = []
        for r in rows:
            prop_text = r[2]
            try:
                prop = json.loads(prop_text) if prop_text else {}
            except Exception:
                prop = {"raw": (prop_text or "")[:400]}
            out.append({
                "id":           r[0],
                "proposed_at":  str(r[1])[:19] if r[1] else None,
                "proposal":     prop,
                "kind":         r[3],
                "model":        r[4],
                "approved":     r[5],
                "shipped_at":   str(r[6])[:19] if r[6] else None,
                "notes":        r[7],
            })
        return jsonify(proposals=out, count=len(out)), 200
    finally:
        try: c.close()
        except Exception: pass
