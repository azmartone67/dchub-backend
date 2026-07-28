"""brain_lane_driver.py — the brain DRIVES the business lanes (2026-07-04)
===========================================================================

Until now the brain was reactive (detectors → findings → investigate →
propose) and the master shells were rule-based (fixed scoring formulas,
fixed action tables). This module closes the loop the 07-04 evolution
session asked for: the brain REASONS about each business lane and picks
the next action itself, grounded in RAG recall of its own past decisions
and their measured outcomes.

THE CYCLE (per tick)
--------------------
  VERIFY  — score the previous unverified decision per lane against the
            lane's main KPI now vs then (improved / flat / regressed) and
            stamp it into the ledger. Verified rows are re-embedded into
            the RAG corpus (fresh_col=verified_at) — the self-learning
            loop: tomorrow's REASON recalls today's outcome.
  SENSE   — deterministic KPI pull per lane (DB + loopback endpoints).
  RECALL  — routes.brain_rag.retrieve_lessons + retrieve_context, in
            process (no HTTP): past lane decisions + outcomes, autopilot
            lessons, related findings.
  REASON  — ONE structured-output Claude call per selected lane (worst
            lanes first, BRAIN_LANE_DRIVER_LANES_PER_TICK, default 2).
            Fable-tier via routes.brain_models (honors
            DCHUB_BRAIN_PREFER_FABLE). output_config.format json_schema
            via routes.brain_llm_structured (fail-soft ladder built in).
            The schema gives the model a FIRST-CLASS "stop" action — the
            brain-intelligence-roadmap "learns to STOP not STEER"
            philosophy as an API contract, not a prompt suggestion.
  ACT     — dispatch the chosen action from a CLOSED catalog of existing,
            already-safe endpoints (or upsert a brain_finding proposal
            for anything needing code/human work). Never posts
            externally itself; caps per tick and per day.
  LEDGER  — brain_lane_decisions row per decision (kpi snapshot,
            diagnosis, action, expectation, confidence, dispatch result).
            Registered as a RAG lesson corpus in routes/brain_rag.py.

FABLE-5 API NOTES (verified via claude-api skill 2026-07-04)
------------------------------------------------------------
· thinking is ALWAYS ON on fable-5 — we send NO `thinking` param at all
  (an explicit disabled/enabled 400s) and keep max_tokens generous
  because thinking bills against it (the 06-30 trap).
· structured outputs via output_config.format — GA, no beta header —
  through brain_llm_structured.build_messages_body (fail-soft to legacy
  text parse on 400).
· prompt caching: the frozen charter + action catalog + this cycle's
  all-lane KPI table go in ONE system block with cache_control ephemeral.
  Identical across the tick's N lane calls → first call writes, the rest
  read at ~0.1x. Per-lane volatile context (sense detail + recall) rides
  in the user message AFTER the breakpoint.
· sampling params (temperature/top_p/top_k) are never sent (400 on
  fable/opus-4.7+); effort rides in output_config.

LANES
-----
  onboarding — AI agent onboarding (arrive→identify→activate)
  funnel     — MCP funnel growth (paywall→claim→redeem)
  revenue    — revenue capture (paid conversions, session upgrades, keys)
  seo        — Google/Bing/Clarity expansion (indexnow, sitemap, recovery)
  media      — DC Hub media analyst (citation velocity, cadence)

SAFETY
------
  admin-gated · BRAIN_LANE_DRIVER_DISABLED kill · _ACT_DISABLED shadow ·
  BRAIN_LANE_DRIVER_LANES_PER_TICK (default 2) ·
  BRAIN_LANE_DRIVER_DAILY_CAP decisions/day (default 12) ·
  closed action catalog · every probe try/except (dead source → lane
  skipped, never a 500) · refusal stop_reason skips the lane.

ROUTES
------
  POST/GET /api/v1/admin/brain/lane-driver/tick
  GET      /api/v1/admin/brain/lane-driver/state
Cron: brain_lane_driver_6h at 04/10/16/22 UTC (offset from gap shell).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)

brain_lane_driver_bp = Blueprint("brain_lane_driver", __name__)

_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)

_LANES = ("onboarding", "funnel", "revenue", "seo", "media")

# ── CLOSED action catalog: action key → (method-less loopback path | None) ──
# None = ledger/proposal-only actions handled inline. Every dispatchable
# path is an EXISTING endpoint with its own guards; the driver never posts
# to an external service itself.
_ACTIONS = {
    "audience_master_tick":    "/api/v1/admin/audience/master-tick",
    "media_master_tick":       "/api/v1/admin/media/master-tick",
    "indexnow_recent_submit":  "/api/v1/admin/indexnow?recent=1",
    "per_tool_conversion_run": "/api/v1/admin/per-tool-conversion/run",
    "deep_dive_rotate":        "/api/v1/markets/deep-dive/cron?count=10",
    "brain_self_direct_tick":  "/api/v1/brain/self-direct/tick",
    "propose_finding":         None,   # upsert_brain_finding → proposer pipeline
    "stop":                    None,   # explicit no-op; recorded as a decision
}

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string",
                      "description": "One-paragraph read of THIS lane's current state, citing the KPI numbers and any recalled lesson that applies."},
        "action": {"type": "string", "enum": sorted(_ACTIONS.keys()),
                   "description": "Next action from the catalog. Choose 'stop' when the lane is healthy or when past outcomes show the available actions don't move it. Choose 'propose_finding' when the right move needs code or human work."},
        "action_reason": {"type": "string",
                          "description": "Why THIS action over the alternatives, referencing past outcomes when recalled."},
        "proposal_title": {"type": "string",
                           "description": "Only when action=propose_finding: a short imperative title for the finding."},
        "expected_effect": {"type": "string",
                            "description": "The measurable change expected on this lane's main KPI, with a rough horizon."},
        "confidence": {"type": "number",
                       "description": "0..1 subjective confidence the action improves the KPI."},
    },
    "required": ["diagnosis", "action", "action_reason", "expected_effect", "confidence"],
    "additionalProperties": False,
}


# ── auth / kills (house pattern) ──────────────────────────────────────
def _admin_key():
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    import hmac as _hmac
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and _hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("BRAIN_LANE_DRIVER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    return str(os.environ.get("BRAIN_LANE_DRIVER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _lanes_per_tick() -> int:
    try:
        return max(1, min(5, int(os.environ.get("BRAIN_LANE_DRIVER_LANES_PER_TICK", "2"))))
    except Exception:
        return 2


def _daily_cap() -> int:
    try:
        return max(1, int(os.environ.get("BRAIN_LANE_DRIVER_DAILY_CAP", "12")))
    except Exception:
        return 12


# ── plumbing ──────────────────────────────────────────────────────────
def _req(path: str, method: str = "GET", timeout: int = 12) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("X-DC-Probe", "lane-driver")
        req.add_header("User-Agent", "dchub-lane-driver/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return {"ok": True, "http": resp.status,
                        "data": json.loads(resp.read().decode("utf-8", "replace"))}
            except Exception:
                return {"ok": True, "http": resp.status, "data": {}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "data": {}}
    except Exception as e:
        return {"ok": False, "http": None, "error": str(e)[:120], "data": {}}


def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _q1(sql: str, params: tuple = ()):  # one row, never raises
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_lane_decisions (
                    id            SERIAL PRIMARY KEY,
                    decided_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    lane          TEXT NOT NULL,
                    kpi           JSONB,
                    kpi_main      NUMERIC,
                    diagnosis     TEXT,
                    action        TEXT,
                    action_reason TEXT,
                    expected_effect TEXT,
                    confidence    NUMERIC,
                    dispatched    BOOLEAN,
                    dispatch_http INTEGER,
                    outcome       TEXT,
                    outcome_note  TEXT,
                    verified_at   TIMESTAMPTZ
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── SENSE (deterministic, per lane) ───────────────────────────────────
def _sense_onboarding() -> dict:
    row = _q1("""
        SELECT COUNT(DISTINCT agent_id) FILTER (WHERE created_at > NOW() - INTERVAL '7 days'),
               COUNT(DISTINCT agent_id) FILTER (WHERE created_at <= NOW() - INTERVAL '7 days'
                                                AND created_at > NOW() - INTERVAL '14 days'),
               COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days')
        FROM mcp_calls_identity
        WHERE is_real_external AND created_at > NOW() - INTERVAL '30 days'
    """)
    if not row:
        return {"error": "db", "kpi_main": 0.0}
    a7, aprev, calls7 = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    return {"agents_7d": a7, "agents_prior_7d": aprev, "real_calls_7d": calls7,
            "kpi_main": float(a7)}


def _sense_funnel() -> dict:
    sd = (_req("/api/v1/admin/mcp/high-intent/step-drop").get("data") or {})
    paywall = int(float(sd.get("paywall_sessions") or 0))
    redeemed = 0
    for st in (sd.get("steps") or []):
        if "redeem" in str(st.get("step", "")).lower():
            redeemed = int(float(st.get("count") or 0))
            break
    rate = (redeemed / paywall) if paywall else 0.0
    return {"paywall_sessions_30d": paywall, "claims_redeemed_30d": redeemed,
            "redeem_rate": round(rate, 3), "killer_step": sd.get("killer_step"),
            "kpi_main": float(redeemed)}


def _sense_revenue() -> dict:
    row = _q1("""
        SELECT (SELECT COUNT(*) FROM mcp_conversions WHERE created_at > NOW() - INTERVAL '30 days'),
               (SELECT COUNT(*) FROM mcp_session_upgrades),
               (SELECT COUNT(*) FROM mcp_dev_keys WHERE status = 'active' AND tier IN ('paid', 'pro', 'developer', 'enterprise'))
    """)
    if not row:
        return {"error": "db", "kpi_main": 0.0}
    conv, upg, paid_keys = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    return {"conversions_30d": conv, "session_upgrades_total": upg,
            "active_paid_keys": paid_keys, "kpi_main": float(conv)}


def _sense_seo() -> dict:
    idx = (_req("/api/v1/admin/indexnow").get("data") or {})
    last = idx.get("last") or {}
    hours = None
    try:
        ts = str(last.get("at") or "").replace("Z", "+00:00")
        if ts:
            hours = round((datetime.now(timezone.utc)
                           - datetime.fromisoformat(ts)).total_seconds() / 3600.0, 1)
    except Exception:
        pass
    return {"indexnow_hours_since_submit": hours,
            "indexnow_last_status": last.get("status"),
            "indexnow_last_submitted": last.get("submitted"),
            "kpi_main": float(-(hours if hours is not None else 999.0))}


def _sense_media() -> dict:
    north = (_req("/api/v1/media/north-star").get("data") or {})
    v7 = int(float(north.get("citation_velocity_7d") or 0))
    v30 = int(float(north.get("citation_velocity_30d") or 0))
    days_since = None
    try:
        recent = north.get("recent") or []
        if recent:
            ts = str(recent[0].get("at") or "").replace("Z", "+00:00")
            days_since = round((datetime.now(timezone.utc)
                                - datetime.fromisoformat(ts)).total_seconds() / 86400.0, 1)
    except Exception:
        pass
    return {"citation_velocity_7d": v7, "citation_velocity_30d": v30,
            "days_since_last_citation": days_since, "kpi_main": float(v7)}


_SENSORS = {
    "onboarding": _sense_onboarding,
    "funnel": _sense_funnel,
    "revenue": _sense_revenue,
    "seo": _sense_seo,
    "media": _sense_media,
}

# Cheap deterministic pre-score (0..1, lower = needier) used ONLY to pick
# which lanes get an LLM decision this tick — the reasoning itself is Claude's.
def _prescore(lane: str, kpi: dict) -> float:
    try:
        if lane == "onboarding":
            return min(1.0, (kpi.get("agents_7d") or 0) / 25.0)
        if lane == "funnel":
            return min(1.0, (kpi.get("redeem_rate") or 0) / 0.5)
        if lane == "revenue":
            return min(1.0, (kpi.get("conversions_30d") or 0) / 15.0)
        if lane == "seo":
            h = kpi.get("indexnow_hours_since_submit")
            return 1.0 if (h is not None and h < 48) else 0.2
        if lane == "media":
            return 1.0 if (kpi.get("citation_velocity_7d") or 0) > 0 else 0.2
    except Exception:
        pass
    return 0.5


# ── RECALL (in-process RAG) ───────────────────────────────────────────
def _recall(lane: str, kpi: dict) -> list:
    out = []
    try:
        from routes.brain_rag import retrieve_lessons, retrieve_context
        q = f"{lane} lane: " + ", ".join(f"{k}={v}" for k, v in kpi.items() if k != "kpi_main")
        for r in (retrieve_lessons(q, k=4) or []):
            out.append({"src": "lesson", "text": str(r.get("text", ""))[:300]})
        for r in (retrieve_context(q, k=3, corpus="brain_findings") or []):
            out.append({"src": "finding", "text": str(r.get("text", ""))[:300]})
    except Exception as e:
        logger.debug("lane-driver recall failed: %s", e)
    return out[:7]


# ── REASON (one structured Claude call per lane) ──────────────────────
_CHARTER = """You are the DC Hub Brain Lane Driver — the autonomous operator of five business lanes for dchub.cloud, the live data-center-intelligence platform AI agents query via MCP.

Your job each cycle: for ONE lane, read its KPIs and the recalled lessons from your own past decisions, then choose the single next action from the closed catalog. You are graded on KPI movement, not activity: a wrong action wastes the lane's action budget until the next cycle, and 'stop' is a respected professional decision when the lane is healthy or when history shows the catalog can't move it. Prefer 'propose_finding' when the real fix needs code or human work — that routes into the brain's proposer pipeline.

ACTION CATALOG (the only legal values):
- audience_master_tick: runs the audience orchestrator (demand/onboarding levers — reach rollups, first-touch nudges).
- media_master_tick: runs the media orchestrator (publishes ONE number-led evergreen if starved; own quality gates).
- indexnow_recent_submit: re-submits recent sitemap URLs to Bing IndexNow (only useful if last submit is stale >48h).
- per_tool_conversion_run: recomputes per-tool conversion + emits brain findings (measurement refresh, cheap).
- deep_dive_rotate: regenerates the 10 stalest market deep-dives (feeds RAG + GEO surfaces).
- brain_self_direct_tick: lets the brain pick one self-directed investigation (costs one model call; daily-capped).
- propose_finding: writes a brain_finding proposal (give proposal_title) for work needing code or humans.
- stop: do nothing this cycle; say why.

CONTEXT — current cycle KPI table (all lanes):
{kpi_table}

Platform facts: north-star = distinct external AI agents/week; conversions close via human-in-the-loop; citation velocity is the authority north-star; Bing recovery in progress after the June slug-churn purge (IndexNow resubmitted 07-04/05); the URL-elicitation experiment owns in-session conversion."""

_USER_TMPL = """LANE THIS CYCLE: {lane}

Current KPIs:
{kpi_json}

Recalled lessons and findings (your own past decisions/outcomes rank first — weigh regressed/flat outcomes heavily before repeating an action):
{recall_block}

Previous decision on this lane (if any) and its verified outcome:
{prev_block}

Decide the single next action for the '{lane}' lane."""


def _reason(lane: str, kpi: dict, recall: list, prev: dict, kpi_table: str) -> dict:
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return {"error": "no_api_key"}
    try:
        from routes.brain_models import brain_model_for
        model = brain_model_for("reasoning")
    except Exception:
        model = os.environ.get("DCHUB_BRAIN_MODEL_REASONING", "claude-opus-4-8")
    try:
        from routes.brain_llm_structured import build_messages_body
        from utils.anthropic_helper import anthropic_messages_url, aig_metadata_headers
    except Exception as e:
        return {"error": f"helpers: {e}"}

    system = _CHARTER.format(kpi_table=kpi_table)
    user = _USER_TMPL.format(
        lane=lane,
        kpi_json=json.dumps({k: v for k, v in kpi.items() if k != "kpi_main"}, sort_keys=True),
        recall_block="\n".join(f"- [{r['src']}] {r['text']}" for r in recall) or "(none recalled)",
        prev_block=json.dumps(prev, sort_keys=True, default=str) if prev else "(first decision on this lane)",
    )
    body, applied = build_messages_body(model, system, [{"role": "user", "content": user}],
                                        max_tokens=16000, schema=_DECISION_SCHEMA)
    # Prompt caching: the charter (+ per-cycle KPI table) is byte-identical
    # across this tick's lane calls — cache it so calls 2..N read at ~0.1x.
    # (Fable-5 min cacheable prefix is 2048 tokens; smaller prefixes silently
    # skip caching, which is fine.)
    try:
        if isinstance(body.get("system"), str):
            body["system"] = [{"type": "text", "text": body["system"],
                               "cache_control": {"type": "ephemeral"}}]
    except Exception:
        pass
    # Effort control (no thinking param on fable — always-on; no sampling params).
    oc = body.get("output_config") or {}
    oc["effort"] = os.environ.get("BRAIN_LANE_DRIVER_EFFORT", "high")
    body["output_config"] = oc

    import requests as _rq
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        headers.update(aig_metadata_headers("brain-lane-driver"))
    except Exception:
        pass
    try:
        resp = _rq.post(anthropic_messages_url(), headers=headers, json=body, timeout=180)
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}", "body": resp.text[:200], "model": model}
        data = resp.json()
        if data.get("stop_reason") == "refusal":
            return {"error": "refusal", "model": model}
        text = next((b.get("text", "") for b in (data.get("content") or [])
                     if b.get("type") == "text"), "")
        decision = json.loads(text)
        decision["_model"] = model
        decision["_structured"] = applied
        usage = data.get("usage") or {}
        decision["_cache_read"] = usage.get("cache_read_input_tokens")
        try:
            from routes.brain_llm_structured import record_llm_usage
            record_llm_usage("brain-lane-driver", model, data)
        except Exception:
            pass
        return decision
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:140]}", "model": model}


# ── ACT ───────────────────────────────────────────────────────────────
def _act(lane: str, decision: dict) -> dict:
    action = str(decision.get("action") or "stop")
    if action not in _ACTIONS:
        return {"dispatched": False, "note": f"unknown action {action!r} → treated as stop"}
    if _act_disabled():
        return {"dispatched": False, "note": "BRAIN_LANE_DRIVER_ACT_DISABLED (shadow)"}
    if action == "stop":
        return {"dispatched": False, "note": "explicit stop"}
    if action == "propose_finding":
        try:
            from routes.brain_findings_writer import upsert_brain_finding
            c = _conn()
            if c is None:
                return {"dispatched": False, "note": "no_db"}
            try:
                # qa-0704b: the canonical writer savepoint-wraps every op and
                # NEVER raises — under an autocommit connection the savepoints
                # fail and it cleanly returns "skipped" (the same
                # SAVEPOINT-in-autocommit trap as the per-tool module, one
                # layer deeper). Transactional conn + trust the return value,
                # exactly as the writer's docstring instructs.
                c.autocommit = False
                with c.cursor() as cur:
                    result = upsert_brain_finding(
                        cur,
                        issue="lane_driver_proposal",
                        url=f"/api/v1/admin/brain/lane-driver/state#{lane}",
                        count=1,
                        detail=(f"[{lane}] {decision.get('proposal_title') or 'proposal'} — "
                                f"{str(decision.get('diagnosis'))[:400]} · expected: "
                                f"{str(decision.get('expected_effect'))[:200]}"),
                        detector="brain_lane_driver",
                        status="open")
                c.commit()
                ok = result in ("inserted", "updated")
                return {"dispatched": ok, "note": f"finding {result}"}
            finally:
                try: c.close()
                except Exception: pass
        except Exception as e:
            return {"dispatched": False, "note": f"finding failed: {str(e)[:100]}"}
    # endpoint dispatch
    path = _ACTIONS[action]
    r = _req(path, method="POST", timeout=8)
    return {"dispatched": True, "http": r.get("http"), "note": f"POST {path}"}


# ── LEDGER + VERIFY ───────────────────────────────────────────────────
def _prev_decision(lane: str):
    return _q1("""
        SELECT id, decided_at, action, expected_effect, kpi_main, outcome
        FROM brain_lane_decisions WHERE lane = %s
        ORDER BY id DESC LIMIT 1
    """, (lane,))


def _verify_lane(lane: str, kpi_main_now: float) -> dict | None:
    row = _q1("""
        SELECT id, decided_at, action, kpi_main FROM brain_lane_decisions
        WHERE lane = %s AND outcome IS NULL
          AND decided_at < NOW() - INTERVAL '3 hours'
        ORDER BY id DESC LIMIT 1
    """, (lane,))
    if not row:
        return None
    dec_id, decided_at, action, kpi_then = row[0], row[1], row[2], float(row[3] or 0)
    delta = kpi_main_now - kpi_then
    outcome = "improved" if delta > 0 else ("regressed" if delta < 0 else "flat")
    note = (f"kpi_main {kpi_then} → {kpi_main_now} ({'+' if delta >= 0 else ''}{round(delta, 2)}) "
            f"in the window after action '{action}'")
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("""UPDATE brain_lane_decisions
                           SET outcome = %s, outcome_note = %s, verified_at = NOW()
                           WHERE id = %s""", (outcome, note, dec_id))
        c.commit()
        return {"decision_id": dec_id, "outcome": outcome, "note": note}
    except Exception:
        note_swallowed_write("brain_lane_decisions", where="brain_lane_driver._verify_lane")
        return None
    finally:
        try: c.close()
        except Exception: pass


def _persist(lane: str, kpi: dict, decision: dict, act: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_lane_decisions
                  (lane, kpi, kpi_main, diagnosis, action, action_reason,
                   expected_effect, confidence, dispatched, dispatch_http)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (lane, json.dumps(kpi, default=str), kpi.get("kpi_main"),
                  str(decision.get("diagnosis"))[:2000], decision.get("action"),
                  str(decision.get("action_reason"))[:1000],
                  str(decision.get("expected_effect"))[:600],
                  decision.get("confidence"),
                  bool(act.get("dispatched")), act.get("http")))
        c.commit()
        return True
    except Exception:
        note_swallowed_write("brain_lane_decisions", where="brain_lane_driver._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


def _decisions_today() -> int:
    row = _q1("SELECT COUNT(*) FROM brain_lane_decisions WHERE decided_at::date = CURRENT_DATE")
    return int(row[0]) if row else 0


# ── ROUTES ────────────────────────────────────────────────────────────
@brain_lane_driver_bp.route("/api/v1/admin/brain/lane-driver/tick", methods=["POST", "GET"])
def lane_driver_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="BRAIN_LANE_DRIVER_DISABLED"), 200
    started = time.time()
    _ensure_tables()

    # SENSE all lanes (cheap) — also feeds VERIFY and the shared KPI table.
    sensed, verified = {}, []
    for lane in _LANES:
        try:
            sensed[lane] = _SENSORS[lane]()
        except Exception as e:
            sensed[lane] = {"error": str(e)[:100], "kpi_main": 0.0}
        v = _verify_lane(lane, float(sensed[lane].get("kpi_main") or 0.0))
        if v:
            verified.append({"lane": lane, **v})

    remaining = max(0, _daily_cap() - _decisions_today())
    n = min(_lanes_per_tick(), remaining)
    # qa-0704c: per-lane cooldown — a lane decided on within the last 5h is
    # not re-decided, so heartbeat re-fires inside a cron window can't spend
    # the day's budget re-litigating the same two lanes (07-04: 4 fires in
    # 12min; the driver itself chose stop/stop by fire #4, but the budget
    # was already burned).
    cooled = []
    for l in _LANES:
        row = _q1("""SELECT 1 FROM brain_lane_decisions
                     WHERE lane = %s AND decided_at > NOW() - INTERVAL '5 hours'
                     LIMIT 1""", (l,))
        if not row:
            cooled.append(l)
    ranked = sorted(cooled or [], key=lambda l: _prescore(l, sensed[l]))
    selected = ranked[:n]

    kpi_table = "\n".join(
        f"  {l}: " + json.dumps({k: v for k, v in sensed[l].items() if k != 'kpi_main'},
                                sort_keys=True, default=str)
        for l in _LANES)

    decisions = []
    for lane in selected:
        prev_row = _prev_decision(lane)
        prev = (dict(zip(("id", "decided_at", "action", "expected_effect", "kpi_main", "outcome"),
                         prev_row)) if prev_row else None)
        recall = _recall(lane, sensed[lane])
        decision = _reason(lane, sensed[lane], recall, prev, kpi_table)
        if decision.get("error"):
            decisions.append({"lane": lane, "skipped": decision["error"],
                              "model": decision.get("model")})
            continue
        act = _act(lane, decision)
        persisted = _persist(lane, sensed[lane], decision, act)
        decisions.append({
            "lane": lane, "action": decision.get("action"),
            "confidence": decision.get("confidence"),
            "diagnosis": str(decision.get("diagnosis"))[:220],
            "dispatched": act.get("dispatched"), "act_note": act.get("note"),
            "persisted": persisted, "model": decision.get("_model"),
            "structured": decision.get("_structured"),
            "cache_read_tokens": decision.get("_cache_read"),
            "recall_used": len(recall),
        })

    return jsonify(ok=True, generated_at=datetime.now(timezone.utc).isoformat(),
                   lanes_sensed={l: sensed[l] for l in _LANES},
                   selected=selected, decisions=decisions, verified=verified,
                   decisions_today=_decisions_today(), daily_cap=_daily_cap(),
                   ms=int((time.time() - started) * 1000)), 200


@brain_lane_driver_bp.route("/api/v1/admin/brain/lane-driver/state", methods=["GET"])
def lane_driver_state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    rows = []
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""SELECT decided_at, lane, action, confidence, dispatched,
                                      outcome, outcome_note, diagnosis
                               FROM brain_lane_decisions ORDER BY id DESC LIMIT 30""")
                for r in cur.fetchall():
                    rows.append({"at": str(r[0]), "lane": r[1], "action": r[2],
                                 "confidence": float(r[3]) if r[3] is not None else None,
                                 "dispatched": r[4], "outcome": r[5],
                                 "outcome_note": r[6], "diagnosis": (r[7] or "")[:200]})
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    return jsonify(ok=True, decisions=rows), 200
