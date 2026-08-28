"""
AI-Adoption Master Shell — one orchestrated loop whose single north-star is:
   *distinct external AI agents that call DC Hub / week*.

You cannot retrain a frontier model from a backend. What you CAN do — and what
this shell chains into one cycle — is maximise the probability that any AI
agent, when asked a data-center / energy question, *discovers* and *calls*
DC Hub's MCP tools. Three levers, mirrored as tiers:

  Tier 1  MEASURE       north-star = weekly distinct external agents (reach_weekly
                        rollup) + registry/self coverage scorecard.
  Tier 2  DISCOVERY     close the MCP-registry coverage gap. DRAFT/QUEUE the
                        missing-registry submissions (paste-ready packages already
                        served by /api/v1/admin/outreach/draft-submissions).
                        Auto-submit stays OFF by default — most registries are
                        PR/form (human-in-loop), and this is the pattern the
                        effect-aware quarantine benched for unreliability.
  Tier 3  LEGIBILITY    keep the self-describing surfaces (llms.txt, MCP manifest,
                        ai-agents.json) reachable + preview the next GEO answer
                        page (dry — real publish stays behind geo_autopublish's
                        own GEO_AUTOPUBLISH_ENABLED gate).
  Tier 4  REINFORCEMENT which platform probes but doesn't convert → where to tune
                        the tool-instruction copy that agents read at tool-list
                        time (in-session reinforcement — the one lever that is
                        already 100% ours).
  Tier 5  VERIFY        delta vs the previous snapshot: did the north-star move?

This orchestrates existing pieces (ai_reach_rollup, brain_ecosystem_watch,
mcp_registry_outreach, mcp_outreach_drafts, geo_autopublish) — it does not
duplicate them. Every tier is guarded; the tick returns a report and never raises.

Endpoints
  POST /api/v1/admin/ai-adoption/master-tick   — run one cycle (admin-keyed)
  GET  /api/v1/admin/ai-adoption/state         — latest snapshot + 14-pt trend
  GET  /api/v1/admin/ai-adoption/dashboard     — the tracking GUI (admin-keyed)

Kill switch:  AI_ADOPTION_MASTER_DISABLED=1     halts the whole tick.
Write gate :  AI_ADOPTION_SUBMIT_ENABLED=1      lets Tier 2 auto-submit the few
                                                automatable registries (default
                                                off → draft/queue only).
"""
from __future__ import annotations

import hmac
import json
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request
from routes._swallowed_writes import note_swallowed_write

ai_adoption_master_shell_bp = Blueprint("ai_adoption_master_shell", __name__)

# Cap how many registries we auto-submit per tick when the write gate is on —
# belt-and-suspenders against a runaway outbound loop (the benched pattern).
_MAX_SUBMITS_PER_TICK = 3


# ── auth (mirrors audience_master_shell) ──────────────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.headers.get("X-Internal-Key")
           or request.args.get("admin_key")
           or request.cookies.get("dchub_adopt_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("AI_ADOPTION_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _submit_enabled() -> bool:
    return str(os.environ.get("AI_ADOPTION_SUBMIT_ENABLED", "")).lower() in ("1", "true", "yes")


# ── DB (raw autocommit conn — db_utils SKIPS DDL at SKIP_DDL=1) ───────
def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_adoption_snapshots (
                    id                  SERIAL PRIMARY KEY,
                    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    distinct_agents_7d  INTEGER,
                    agents_wow_delta    INTEGER,
                    distinct_platforms  INTEGER,
                    registry_covered    INTEGER,
                    registry_total      INTEGER,
                    self_surfaces_ok    INTEGER,
                    self_surfaces_total INTEGER,
                    drafts_queued       INTEGER,
                    submitted           INTEGER,
                    top_platform        TEXT,
                    geo_next_slug       TEXT,
                    detail              JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _prev_snapshot() -> dict | None:
    c = _conn()
    if c is None:
        return None
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM ai_adoption_snapshots ORDER BY id DESC LIMIT 1")
            return cur.fetchone()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


# ── Tier 1 — MEASURE (north-star + coverage) ──────────────────────────
def _reach_two_weeks() -> list:
    """Light read of the last two rows of the reach_weekly rollup (kept fresh by
    its own daily cron). Falls back to recomputing once if the table is empty."""
    c = _conn()
    if c is None:
        return []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Exclude the in-progress calendar week — comparing a partial current
            # week against a full prior week manufactured a fake "-57 WoW" drop
            # (2026-07-23). Only ever WoW two COMPLETE weeks.
            cur.execute("""
                SELECT week_start, distinct_external_ips, distinct_platforms,
                       new_external_ips, requests, per_platform
                FROM reach_weekly
                WHERE week_start < date_trunc('week', now())::date
                ORDER BY week_start DESC LIMIT 2
            """)
            rows = cur.fetchall() or []
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass


def tier1_measure() -> dict:
    out: dict = {"ok": True}

    rows = _reach_two_weeks()
    if not rows:
        # cold start — recompute once, then re-read
        try:
            from routes.ai_reach_rollup import run_reach_rollup
            run_reach_rollup()
        except Exception as e:
            out["reach_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        rows = _reach_two_weeks()

    cur_wk = rows[0] if rows else {}
    prev_wk = rows[1] if len(rows) > 1 else {}
    agents = int(cur_wk.get("distinct_external_ips") or 0)
    prev_agents = int(prev_wk.get("distinct_external_ips") or 0)
    out["distinct_agents_7d"] = agents
    out["agents_prev_week"] = prev_agents
    out["agents_wow_delta"] = agents - prev_agents
    out["distinct_platforms"] = int(cur_wk.get("distinct_platforms") or 0)
    out["new_agents_this_week"] = int(cur_wk.get("new_external_ips") or 0)
    out["week_start"] = str(cur_wk.get("week_start") or "")

    # per_platform is JSONB — may arrive as list or json string
    pp = cur_wk.get("per_platform")
    if isinstance(pp, str):
        try: pp = json.loads(pp)
        except Exception: pp = []
    out["per_platform"] = pp if isinstance(pp, list) else []

    # coverage scorecard (registry + self surfaces)
    try:
        from routes.brain_ecosystem_watch import compute_coverage_scorecard
        cov = compute_coverage_scorecard() or {}
        by_kind = cov.get("by_kind") or {}
        reg = by_kind.get("registry") or {}
        slf = by_kind.get("self") or {}
        out["registry_covered"] = int(reg.get("covered") or 0)
        out["registry_total"] = int(reg.get("total") or 0)
        out["self_surfaces_ok"] = int(slf.get("covered") or 0)
        out["self_surfaces_total"] = int(slf.get("total") or 0)
        out["owner_actions"] = cov.get("owner_actions") or []
    except Exception as e:
        out["coverage_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        out["registry_covered"] = out["registry_total"] = 0
        out["self_surfaces_ok"] = out["self_surfaces_total"] = 0
        out["owner_actions"] = []

    return out


# ── Tier 2 — DISCOVERY (close the registry gap) ───────────────────────
def tier2_discovery(measure: dict) -> dict:
    out: dict = {"ok": True, "submit_enabled": _submit_enabled()}
    # owner_actions from the coverage scorecard IS the actionable missing-registry
    # list; each carries a submit_url + submit_method (pr / form).
    owner_actions = measure.get("owner_actions") or []
    out["missing"] = [
        {"name": a.get("name"), "method": a.get("submit_method"), "url": a.get("submit_url")}
        for a in owner_actions
    ]
    out["drafts_queued"] = len(owner_actions)
    out["draft_source"] = "/api/v1/admin/outreach/draft-submissions"
    out["submitted"] = 0
    # ground the registry-closure push in recalled outreach/submission OUTCOMES
    out["discovery_evidence"] = _rag_ground(
        "MCP registry directory submission outreach outcome — what got us listed",
        k=3, lessons=True,
    )

    # Only the handful of registries with an automatable submit path can be fired
    # autonomously, and only when the write gate is explicitly on. Everything else
    # stays a paste-ready draft for a human (that's why this is safe to arm).
    if _submit_enabled() and owner_actions:
        submitted = []
        try:
            from routes.mcp_registry_outreach import DISCOVERY_TARGETS, _submit_target
            auto = [t for t in DISCOVERY_TARGETS
                    if str(t.get("submit_method", "")).lower() not in ("pr", "form", "manual")]
            for t in auto[:_MAX_SUBMITS_PER_TICK]:
                try:
                    r = _submit_target(t) or {}
                    submitted.append({"target": t.get("name"), "outcome": r.get("outcome")})
                except Exception as e:
                    submitted.append({"target": t.get("name"),
                                      "error": f"{type(e).__name__}: {str(e)[:80]}"})
        except Exception as e:
            out["submit_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        out["submitted"] = len(submitted)
        out["submit_detail"] = submitted

    return out


# ── Tier 3 — LEGIBILITY (self-surfaces + GEO preview) ─────────────────
def tier3_legibility(measure: dict) -> dict:
    out: dict = {"ok": True}
    out["self_surfaces_ok"] = measure.get("self_surfaces_ok", 0)
    out["self_surfaces_total"] = measure.get("self_surfaces_total", 0)
    out["self_surfaces_healthy"] = (
        out["self_surfaces_total"] > 0
        and out["self_surfaces_ok"] >= out["self_surfaces_total"]
    )

    # preview next uncovered GEO answer page (dry — never publishes here; a real
    # publish only happens if geo_autopublish's own GEO_AUTOPUBLISH_ENABLED is on)
    try:
        from routes.geo_autopublish import autopublish_next
        g = autopublish_next(dry=True) or {}
        considered = g.get("considered") or {}
        out["geo_next_slug"] = considered.get("slug") if isinstance(considered, dict) else None
        out["geo_next_title"] = considered.get("title") if isinstance(considered, dict) else None
        out["geo_acted"] = bool(g.get("acted"))
    except Exception as e:
        out["geo_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        out["geo_next_slug"] = None
        out["geo_acted"] = False

    return out


# ── RAG grounding helper (r-rag-adoption 2026-07-04) ──────────────────
# Best-effort: recall past-outcome lessons / corpus evidence so a decision cites
# WHY. Fail-soft → [] (never breaks a tick). Gate on `cosine` — `score` is the
# rerank cross-encoder scale and would silently disarm a cosine-tuned floor
# (r-rag-cosine-passthrough). retrieve_* opens its own conn + embeds per call, so
# keep k small and call it at most a couple times per tick.
_RAG_MIN_COSINE = 0.30

def _rag_ground(query: str, k: int = 4, corpus=None, lessons: bool = False) -> list:
    import os as _os
    if _os.getenv("RAG_TOOLDATA_DISABLED") == "1":  # one lever kills all RAG grounding
        return []
    try:
        from routes.brain_rag import retrieve_context, retrieve_lessons, _hydrate
    except Exception:
        return []
    try:
        hits = retrieve_lessons(query, k=k) if lessons else retrieve_context(query, k=k, corpus=corpus)
    except Exception:
        return []
    if not hits:
        return []
    try:
        hits = _hydrate(hits)
    except Exception:
        pass
    out = []
    for h in hits or []:
        try:
            if float(h.get("cosine") or 0) < _RAG_MIN_COSINE:
                continue
        except Exception:
            continue
        cite = h.get("cite") or {}
        out.append({
            "source": h.get("source_table"),
            "kind": h.get("kind"),
            "cosine": round(float(h.get("cosine") or 0), 3),
            "text": (h.get("text") or "").strip()[:280],
            "title": cite.get("title"),
            "url": cite.get("url"),
        })
    return out


# ── Tier 4 — REINFORCEMENT (where to tune the tool-instruction copy) ──
def tier4_reinforcement(measure: dict) -> dict:
    out: dict = {"ok": True, "reinforcement_evidence": []}
    pp = measure.get("per_platform") or []
    # normalise: expect dicts with platform_id/agents/requests
    norm = []
    for p in pp:
        if not isinstance(p, dict):
            continue
        norm.append({
            "platform": p.get("platform_id") or p.get("platform") or "unknown",
            "agents": int(p.get("agents") or 0),
            "requests": int(p.get("requests") or 0),
        })
    total_req = sum(p["requests"] for p in norm) or 0
    out["platforms_ranked"] = sorted(norm, key=lambda x: x["requests"], reverse=True)[:8]

    if norm and total_req:
        top = out["platforms_ranked"][0]
        out["top_platform"] = top["platform"]
        out["top_platform_share"] = round(top["requests"] / total_req, 3)
        # a platform with lots of requests but very few distinct agents is probing
        # heavily without converting → highest-leverage place to tune the copy.
        leak = sorted(
            [p for p in norm if p["requests"] >= 5],
            key=lambda x: (x["agents"] / x["requests"]) if x["requests"] else 1.0,
        )
        out["conversion_leak"] = leak[0]["platform"] if leak else None
        tgt = out["conversion_leak"] or out["top_platform"]
        # ground the copy-tuning target in past reinforcement/outreach OUTCOMES so
        # the shell recalls what already worked/failed for this platform instead of
        # re-recommending blind (learns to STOP, not just STEER).
        out["reinforcement_evidence"] = _rag_ground(
            f"tune MCP tool-instruction copy for {tgt}; convert probing agents into repeat callers",
            k=4, lessons=True,
        )
        out["recommendation"] = (
            f"Tune MCP tool-instruction copy for '{tgt}' — it drives the most "
            f"probe volume relative to distinct agents; sharper first-call guidance "
            f"is the in-session lever most likely to convert it to repeat callers."
        )
    else:
        out["top_platform"] = None
        out["top_platform_share"] = 0.0
        out["conversion_leak"] = None
        out["recommendation"] = "Not enough per-platform reach data this week to target copy."

    return out


# ── Tier 5 — VERIFY (delta vs previous snapshot; read BEFORE persist) ─
def tier5_verify(measure: dict) -> dict:
    prev = _prev_snapshot()
    if not prev:
        return {"ok": True, "first_run": True, "note": "no prior snapshot to compare"}
    d_agents = int(measure.get("distinct_agents_7d") or 0) - int(prev.get("distinct_agents_7d") or 0)
    d_cov = int(measure.get("registry_covered") or 0) - int(prev.get("registry_covered") or 0)
    return {
        "ok": True,
        "north_star_delta_vs_last_tick": d_agents,
        "registry_coverage_delta": d_cov,
        "trend": ("up" if d_agents > 0 else "flat" if d_agents == 0 else "down"),
        "prev_computed_at": str(prev.get("computed_at") or ""),
    }


# ── persist ───────────────────────────────────────────────────────────
def _persist(measure: dict, discovery: dict, legibility: dict, reinforcement: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        detail = {
            "discovery": discovery,
            "legibility": legibility,
            "reinforcement": reinforcement,
            "per_platform": measure.get("per_platform"),
        }
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_adoption_snapshots
                  (distinct_agents_7d, agents_wow_delta, distinct_platforms,
                   registry_covered, registry_total, self_surfaces_ok,
                   self_surfaces_total, drafts_queued, submitted, top_platform,
                   geo_next_slug, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (
                measure.get("distinct_agents_7d"),
                measure.get("agents_wow_delta"),
                measure.get("distinct_platforms"),
                measure.get("registry_covered"),
                measure.get("registry_total"),
                measure.get("self_surfaces_ok"),
                measure.get("self_surfaces_total"),
                discovery.get("drafts_queued"),
                discovery.get("submitted"),
                reinforcement.get("top_platform"),
                legibility.get("geo_next_slug"),
                json.dumps(detail, default=str),
            ))
        return True
    except Exception:
        note_swallowed_write("ai_adoption_snapshots", where="ai_adoption_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── the tick ──────────────────────────────────────────────────────────
@ai_adoption_master_shell_bp.route("/api/v1/admin/ai-adoption/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="AI_ADOPTION_MASTER_DISABLED"), 200
    started = time.time()

    measure = tier1_measure()
    discovery = tier2_discovery(measure)
    legibility = tier3_legibility(measure)
    reinforcement = tier4_reinforcement(measure)
    verify = tier5_verify(measure)                 # delta vs PREVIOUS (before insert)
    persisted = _persist(measure, discovery, legibility, reinforcement)

    agents = measure.get("distinct_agents_7d", 0)
    delta = measure.get("agents_wow_delta", 0)
    sign = "+" if delta >= 0 else ""
    headline = (
        f"{agents} agents/wk ({sign}{delta} WoW) · "
        f"registry {measure.get('registry_covered', 0)}/{measure.get('registry_total', 0)} · "
        f"{discovery.get('drafts_queued', 0)} submissions queued · "
        f"tune copy → {reinforcement.get('conversion_leak') or reinforcement.get('top_platform') or 'n/a'}"
    )
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        headline=headline,
        north_star="distinct external AI agents / week",
        tier1_measure=measure,
        tier2_discovery=discovery,
        tier3_legibility=legibility,
        tier4_reinforcement=reinforcement,
        tier5_verify=verify,
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@ai_adoption_master_shell_bp.route("/api/v1/admin/ai-adoption/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM ai_adoption_snapshots ORDER BY id DESC LIMIT 14")
            rows = cur.fetchall() or []
        return jsonify(latest=(rows[0] if rows else None),
                       trend=list(reversed(rows)), count=len(rows)), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:120]}"), 500
    finally:
        try: c.close()
        except Exception: pass


# ── the tracking GUI ──────────────────────────────────────────────────
_ADMIN_ONLY_HTML = (
    "<!doctype html><meta charset=utf-8><title>AI-Adoption · admin only</title>"
    "<body style='font:15px system-ui;background:#0a0a12;color:#9ca3af;padding:40px'>"
    "<h2 style='color:#fff'>Admin key required</h2>"
    "<p>Open with <code>?admin_key=…</code> (DCHUB_ADMIN_KEY).</p>"
)

_PAGE_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>AI-Adoption Master Shell · DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--surface2:#0d0e16;--bd:#1f2030;--tx:#fff;
 --tx2:#9ca3af;--tx3:#6b7280;--indigo:#6366f1;--violet:#a855f7;--green:#10b981;
 --amber:#f59e0b;--red:#ef4444;--grad:linear-gradient(135deg,#6366f1,#a855f7);
 --mono:'JetBrains Mono','SF Mono',ui-monospace,monospace;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
.kicker{font-family:var(--mono);font-size:11px;letter-spacing:.14em;color:var(--tx2);
 text-transform:uppercase;display:flex;align-items:center;gap:8px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);
 box-shadow:0 0 0 0 rgba(16,185,129,.6);animation:p 2s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(16,185,129,.6)}70%{box-shadow:0 0 0 8px rgba(16,185,129,0)}100%{box-shadow:0 0 0 0 rgba(16,185,129,0)}}
h1{font-size:26px;margin:10px 0 2px;background:var(--grad);-webkit-background-clip:text;
 -webkit-text-fill-color:transparent;background-clip:text}
.sub{color:var(--tx2);font-size:14px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--bd);border-radius:14px;padding:16px 18px}
.card .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;color:var(--tx3);text-transform:uppercase}
.card .val{font-size:30px;font-weight:700;margin-top:6px;line-height:1}
.card .delta{font-family:var(--mono);font-size:12px;margin-top:6px}
.up{color:var(--green)}.down{color:var(--red)}.flat{color:var(--tx3)}
.panel{background:var(--surface);border:1px solid var(--bd);border-radius:14px;padding:18px 20px;margin-bottom:18px}
.panel h2{font-size:14px;margin:0 0 12px;color:var(--tx);letter-spacing:.02em}
.tier{font-family:var(--mono);font-size:10px;color:var(--violet);letter-spacing:.1em}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--surface2)}
th{color:var(--tx3);font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:500}
td a{color:var(--indigo);text-decoration:none}td a:hover{text-decoration:underline}
.rec{color:var(--amber);font-size:13.5px;line-height:1.5}
.spark{display:flex;align-items:flex-end;gap:3px;height:44px;margin-top:8px}
.spark i{flex:1;background:var(--grad);border-radius:2px 2px 0 0;min-height:2px;opacity:.85}
.foot{color:var(--tx3);font-size:11.5px;font-family:var(--mono);margin-top:8px}
.pill{display:inline-block;font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:999px;border:1px solid var(--bd);color:var(--tx2)}
.pill.on{color:var(--green);border-color:rgba(16,185,129,.4)}
.pill.off{color:var(--amber);border-color:rgba(245,158,11,.4)}
.err{color:var(--red);font-family:var(--mono);font-size:12px}
</style></head><body><div class="wrap">
<div class="kicker"><span class="pulse"></span>DC HUB · BRAIN · AI-ADOPTION MASTER SHELL</div>
<h1>Get every AI to reach for DC Hub</h1>
<div class="sub">North-star: <b>distinct external AI agents that call DC Hub / week</b>.
 One loop — measure · discover · make legible · reinforce · verify.
 <span id="gates"></span></div>

<div class="cards" id="cards"></div>

<div class="panel">
  <div class="tier">TIER 1 · MEASURE</div>
  <h2>North-star trend (weekly distinct external agents)</h2>
  <div class="spark" id="spark"></div>
  <div class="foot" id="sparkfoot"></div>
</div>

<div class="panel">
  <div class="tier">TIER 2 · DISCOVERY</div>
  <h2>Registry submissions queued <span id="q"></span></h2>
  <table id="missing"><thead><tr><th>Registry</th><th>Method</th><th>Submit</th></tr></thead><tbody></tbody></table>
  <div class="foot">Paste-ready packages: <code>/api/v1/admin/outreach/draft-submissions</code></div>
</div>

<div class="panel">
  <div class="tier">TIER 4 · REINFORCEMENT</div>
  <h2>Where to tune the tool-instruction copy</h2>
  <div class="rec" id="rec"></div>
  <table id="platforms" style="margin-top:12px"><thead><tr><th>Platform</th><th>Agents</th><th>Requests</th></tr></thead><tbody></tbody></table>
</div>

<div class="foot" id="stamp"></div>
</div>
<script>
const KEY = new URLSearchParams(location.search).get('admin_key') || '';
const authq = u => KEY ? u + (u.includes('?')?'&':'?') + 'admin_key=' + encodeURIComponent(KEY) : u;
const authh = () => KEY ? {'X-Admin-Key': KEY} : {};
const el = id => document.getElementById(id);
const esc = s => String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function card(lbl,val,delta){
  let d='';
  if(delta!==undefined && delta!==null){
    const cls = delta>0?'up':delta<0?'down':'flat';
    const sign = delta>0?'+':'';
    d=`<div class="delta ${cls}">${sign}${delta} vs last week</div>`;
  }
  return `<div class="card"><div class="lbl">${lbl}</div><div class="val">${val}</div>${d}</div>`;
}

async function load(){
  let j;
  try{
    const r = await fetch(authq('/api/v1/admin/ai-adoption/state'), {headers:authh()});
    j = await r.json();
  }catch(e){ el('cards').innerHTML='<div class="err">state fetch failed: '+esc(e)+'</div>'; return; }
  const L = j.latest;
  if(!L){ el('cards').innerHTML='<div class="foot">No tick has run yet. POST /api/v1/admin/ai-adoption/master-tick</div>'; return; }
  const det = L.detail || {};

  el('cards').innerHTML =
    card('Agents / week', L.distinct_agents_7d ?? '—', L.agents_wow_delta) +
    card('Platforms', L.distinct_platforms ?? '—') +
    card('Registry coverage', (L.registry_covered ?? 0)+' / '+(L.registry_total ?? 0)) +
    card('Self-surfaces OK', (L.self_surfaces_ok ?? 0)+' / '+(L.self_surfaces_total ?? 0)) +
    card('Submissions queued', L.drafts_queued ?? 0);

  // spark from trend
  const T = (j.trend||[]).filter(x=>x.distinct_agents_7d!=null);
  const mx = Math.max(1, ...T.map(x=>x.distinct_agents_7d||0));
  el('spark').innerHTML = T.map(x=>`<i style="height:${Math.round((x.distinct_agents_7d||0)/mx*100)}%" title="${esc(x.computed_at)}: ${x.distinct_agents_7d}"></i>`).join('');
  el('sparkfoot').textContent = T.length? `${T.length} ticks · peak ${mx} agents/wk` : 'awaiting history';

  // discovery
  const miss = (det.discovery && det.discovery.missing) || [];
  el('q').innerHTML = `<span class="pill">${miss.length} open</span>`;
  el('missing').querySelector('tbody').innerHTML = miss.length
    ? miss.map(m=>`<tr><td>${esc(m.name)}</td><td>${esc(m.method||'')}</td><td><a href="${esc(m.url)}" target="_blank" rel="noopener">submit ↗</a></td></tr>`).join('')
    : '<tr><td colspan=3 class="foot">Full coverage — no open submissions.</td></tr>';

  // reinforcement
  const rf = det.reinforcement || {};
  el('rec').textContent = rf.recommendation || '—';
  const pr = rf.platforms_ranked || [];
  el('platforms').querySelector('tbody').innerHTML = pr.length
    ? pr.map(p=>`<tr><td>${esc(p.platform)}</td><td>${p.agents}</td><td>${p.requests}</td></tr>`).join('')
    : '<tr><td colspan=3 class="foot">No per-platform reach yet.</td></tr>';

  // gates
  const sub = (det.discovery && det.discovery.submit_enabled);
  el('gates').innerHTML = `<span class="pill ${sub?'on':'off'}">auto-submit ${sub?'ON':'draft-only'}</span>`;

  el('stamp').textContent = 'Snapshot '+esc(L.computed_at)+' · auto-refresh 60s';
}
load(); setInterval(load, 60000);
</script></body></html>"""


@ai_adoption_master_shell_bp.route("/api/v1/admin/ai-adoption/dashboard", methods=["GET"])
def dashboard():
    if not _admin_ok():
        return Response(_ADMIN_ONLY_HTML, mimetype="text/html", status=403)
    return Response(_PAGE_HTML, mimetype="text/html")
