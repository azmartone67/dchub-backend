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
  RECALL  — routes.brain_rag.recall_negative_lessons (what we got WRONG:
            refuted/retracted claims, rejected proposals, failed fixes —
            agentic-loop #65 part C, ranked first) + retrieve_lessons +
            retrieve_context, in process (no HTTP): past lane decisions +
            outcomes, autopilot lessons, related findings.
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

# ── ★2026-08-29 lane 3 (effector-read) ───────────────────────────────────
#
# The catalog above is EIGHT verbs, six of which run an existing orchestrator
# and none of which changes the product. Meanwhile routes/squasher_action_classes
# already holds a real effector registry — granted / reversible / verifier_url /
# bound_params / breaker_tripped / runs_ok / consecutive_failed, plus an
# append-only brain_action_class_runs ledger, with facility_dedup_apply at 7
# runs / 0 failures. It was ~70% built and the lane driver simply never read it.
#
# So the driver now SOURCES its verbs from brain_action_classes WHERE granted,
# instead of a second registry being built beside the first.
#
# Two things this deliberately does NOT do:
#
#   1. It does not drop the six tick verbs. The handoff's own sequencing note:
#      drop them first and the driver is left with `stop` alone. They go when
#      a registry verb has a track record on this path, not before.
#   2. It does not reimplement execution. Dispatch delegates to
#      squasher_action_classes.execute_one(), so a registry verb fired from
#      here inherits every guard the drain has: the global ACTION_CLASSES_ENABLED
#      kill, the per-class grant, the reversible/verifier/bound_params grant
#      test re-checked at run time, the breaker, the caps, and the
#      pre-read → claim → ledger → mutate → post-read → verdict order. A
#      second execution path would be a second thing to keep correct.
_EFFECTOR_PREFIX = "effector:"


def effectors_opted_in() -> bool:
    """Is THIS caller allowed to dispatch registry effectors?

    ★2026-08-29, after #3317 shipped. That PR said "with ACTION_CLASSES_ENABLED
    unset the action space is unchanged from today's eight, which is the
    intended default". Measured in production immediately afterwards, the
    assumption was FALSE:

        ACTION_CLASSES_ENABLED         = True
        BRAIN_LANE_DRIVER_ACT_DISABLED = unset (the driver is dispatching)

    So the action space silently became ELEVEN verbs, three of which mutate
    data, and two of those classes (deals_exact_dupe_quarantine,
    news_entity_reresolve) stand at runs_ok=0 — no track record on ANY path.

    The grant that made them eligible was given to the SQUASHER'S DRAIN. #3317
    extended it to a second caller by inheritance, which is not what granting
    a class to one drain means. A grant is per-effector, not per-platform.

    This flag makes the extension an explicit choice. Default OFF: an operator
    turns it on for this caller once they want the lane driver acting through
    the registry, exactly as they turned it on for the drain.
    """
    return str(os.environ.get("BRAIN_LANE_DRIVER_EFFECTORS", "")).strip().lower() \
        in ("1", "true", "yes")


def registry_actions() -> dict:
    """Granted, currently-eligible action classes, as driver verbs.

    Returns {"effector:<class>": cls_row}. Empty on ANY read failure — but the
    caller reports WHY, because "the registry says nothing is granted" and "I
    could not read the registry" are different facts and must not render the
    same way. The opt-in being off is a THIRD distinct fact and reports as its
    own reason.
    """
    if not effectors_opted_in():
        return {"__opt_out__": ("BRAIN_LANE_DRIVER_EFFECTORS is not set — the "
                                "registry grant belongs to the squasher drain; "
                                "this caller opts in separately")}
    try:
        from routes import squasher_action_classes as _ac
    except Exception as e:
        return {"__error__": f"registry import failed: {str(e)[:120]}"}
    if not _ac.enabled():
        # Not an error: the global kill is off by design. Say so explicitly.
        return {"__disabled__": "ACTION_CLASSES_ENABLED is not 1"}
    try:
        with _ac._conn() as conn, conn.cursor() as cur:
            rows = _ac.class_rows(cur)
    except Exception as e:
        return {"__error__": f"registry unreadable: {str(e)[:120]}"}
    out = {}
    for r in rows or []:
        ok, _why = _ac.eligible(r)
        if ok:
            out[_EFFECTOR_PREFIX + str(r.get("class"))] = r
    return out


def available_actions() -> tuple[dict, dict]:
    """(action_key -> path|None, basis). The driver's real action space."""
    acts = dict(_ACTIONS)
    reg = registry_actions()
    basis = {"static": sorted(_ACTIONS.keys()), "registry": [],
             "registry_state": "ok"}
    if "__error__" in reg:
        basis["registry_state"] = reg["__error__"]
    elif "__opt_out__" in reg:
        basis["registry_state"] = reg["__opt_out__"]
    elif "__disabled__" in reg:
        basis["registry_state"] = reg["__disabled__"]
    else:
        for key in reg:
            acts[key] = None          # dispatched via the registry, not a path
        basis["registry"] = sorted(reg.keys())
    return acts, basis


def decision_schema(actions: dict | None = None) -> dict:
    """The decision schema for THIS tick. The enum has to be built per call:
    a module-scope enum can only ever offer the hardcoded eight."""
    keys = sorted((actions or _ACTIONS).keys())
    schema = json.loads(json.dumps(_DECISION_SCHEMA))
    schema["properties"]["action"]["enum"] = keys
    return schema


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
    out = {"agents_7d": a7, "agents_prior_7d": aprev, "real_calls_7d": calls7,
           "kpi_main": float(a7)}
    _withhold_uncomparable_prior(out)
    return out


def _withhold_uncomparable_prior(out: dict) -> None:
    """Null `agents_prior_7d` when the two windows straddle a definition change.

    ★★★ 2026-08-26 — THIS LANE ROLLED ITS OWN WoW AND CALLED A CORRECTION A
    COLLAPSE. `_sense_onboarding` queries mcp_calls_identity directly, so it
    never passed through `_mark_wow_comparability`, which the funnel endpoint
    added on 2026-08-20 for exactly this pair. Live 2026-08-26T21:28Z the funnel
    correctly published `real_external_agents_wow_pct: null` +
    `_withheld: -70.4` + a reason — while this lane handed the model
    `agents_7d 16` and `agents_prior_7d 56` side by side and it did the division
    itself, filing "a -71% WoW collapse back to the all-time trough floor" on
    seven consecutive ticks (08-24 -78% -> 08-26 -71%).

    Nothing collapsed. #202 (2026-08-18 06:31Z) stopped counting DC Hub's own
    GitHub Actions as external demand — 49.9% of `is_real_external` over 60d,
    0.05% after. The CURRENT window is clean; the PRIOR window is wholly
    pre-fix, so the delta compares a corrected population against the one the
    correction removed. `agents_7d` has been FLAT at 15-18 throughout; it is
    `prior_7d` that decays (73 -> 56) as the contaminated window rolls off, and
    the alarm self-clears ~2026-09-01 on its own.

    Withholding the LEVEL, not just a pct, because this consumer is an LLM: give
    it two numbers and it will divide them no matter what a sibling flag says —
    the same lesson `_mark_wow_comparability` learned about renderers ("a flag
    the renderer does not read changes nothing"), one consumer-type further on.
    `agents_7d` and `kpi_main` are untouched: levels are always safe, and
    kpi_main is what the lane is actually graded on.

    Fail-soft: losing the marker must never cost the sense payload.
    """
    try:
        from datetime import timedelta as _td
        from routes.weekly_series import comparability_for_spans
        now = datetime.now(timezone.utc)
        comp = comparability_for_spans([(now - _td(days=7), now),
                                        (now - _td(days=14), now - _td(days=7))])
    except Exception:
        return
    out["agents_prior_7d_comparability"] = comp
    if comp.get("quotable_as_trend"):
        return
    if out.get("agents_prior_7d") is not None:
        out["agents_prior_7d_withheld"] = out["agents_prior_7d"]
        out["agents_prior_7d"] = None
        out["agents_prior_7d_withheld_reason"] = comp.get("means")


# The funnel lane is graded on the AGENT-SIDE chain, never on `claim_redeemed`.
# ★ 2026-08-25: the previous reader walked `steps[]` and broke on the FIRST step
# whose name contained "redeem" — which is `claim_redeemed` (2787), not
# `agent_redeemed` (126). kpi_main therefore tracked a number that cannot fall,
# and the lane reported "stable within the healthy band" for three days while
# the SAME payload carried killer_step=agent_first_call and agent_paid=0.
# Match step names EXACTLY; a renamed step must read 0 (and show up as a
# regression) rather than silently bind to a neighbour.
_FUNNEL_STEPS = ("paywall_sessions", "claims_minted", "claim_redeemed",
                 "agent_redeemed", "agent_key_issued", "agent_first_call",
                 "agent_upsell", "agent_click", "agent_paid",
                 "human_redeemed", "human_key_issued", "human_paid")


def _sense_funnel() -> dict:
    sd = (_req("/api/v1/admin/mcp/high-intent/step-drop").get("data") or {})
    by_name = {}
    for st in (sd.get("steps") or []):
        name = str(st.get("step", ""))
        if name in _FUNNEL_STEPS:
            by_name[name] = int(float(st.get("count") or 0))

    def n(step: str) -> int:
        return int(by_name.get(step, 0))

    paywall = int(float(sd.get("paywall_sessions") or 0)) or n("paywall_sessions")
    claim_redeemed = n("claim_redeemed")
    issued = n("agent_key_issued")
    first_call = n("agent_first_call")

    # ACTIVATION is the funnel's real output: an issued agent key that made a
    # call. Both terms are agent-side and from the same population, so the ratio
    # is honest. We deliberately do NOT publish claim_redeemed/agent_redeemed as
    # a rate: mint-cliff attributes ~81% of never-called keys to
    # `unattributable_no_session`, so that gap is an ATTRIBUTION artifact and
    # folding it into a behavioural rate would read as "the agent left".
    activation_rate = (first_call / issued) if issued else 0.0
    unattributed = max(0, claim_redeemed - n("agent_redeemed") - n("human_redeemed"))

    return {"paywall_sessions_30d": paywall,
            "claim_redeemed_30d": claim_redeemed,
            "agent_redeemed_30d": n("agent_redeemed"),
            "agent_key_issued_30d": issued,
            "agent_first_call_30d": first_call,
            "agent_upsell_30d": n("agent_upsell"),
            "agent_click_30d": n("agent_click"),
            "agent_paid_30d": n("agent_paid"),
            "human_paid_30d": n("human_paid"),
            "redeem_unattributed_30d": unattributed,
            "activation_rate": round(activation_rate, 3),
            "killer_step": sd.get("killer_step"),
            "reading": ("kpi_main is agent_first_call — an issued agent key that "
                        "actually called. claim_redeemed counts CLAIM redemptions, "
                        "most of them unattributable to an agent; it is context, "
                        "never the grade. ★ killer_step is computed over the "
                        "MECHANICAL steps only, and agent_upsell / agent_click / "
                        "agent_paid are all mechanical=False upstream — so the "
                        "money steps can never BE the killer_step no matter how "
                        "badly they read. Judge them from their counts here, not "
                        "from killer_step."),
            "kpi_main": float(first_call)}


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
            # Was redeem_rate/0.5 — with claim-side redeem_rate 0.833 this pinned
            # to 1.0 (maximally healthy), so the neediest lane was picked LAST
            # for reasoning: 5 of 30 decisions vs onboarding's 11. Grade the
            # activation ratio instead.
            return min(1.0, (kpi.get("activation_rate") or 0) / 0.8)
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
_NEGATIVE_RECALL_K = 2     # refuted/retracted claims + rejected proposals, first
_RECALL_CAP = 9            # 2 negative + 4 lessons + 3 findings


def _recall(lane: str, kpi: dict) -> list:
    """RECALL for one lane, best-first: what we got WRONG (agentic-loop #65
    part C — routes.brain_rag.recall_negative_lessons: claims the verifier
    REFUTED / the owner RETRACTED, proposals rejected as duplicates, failed
    fixes), then this lane's own past decisions + outcomes, then related
    findings. Identical texts collapse to one. Fail-soft per source: the
    negative recall rides its own try, so an older brain_rag without the
    helper (or a failing one) never costs the lane its other recall."""
    out = []
    seen = set()

    def _add(src, text):
        t = str(text or "")[:300]
        if t and t not in seen:
            seen.add(t)
            out.append({"src": src, "text": t})

    try:
        from routes.brain_rag import retrieve_lessons, retrieve_context
        q = f"{lane} lane: " + ", ".join(f"{k}={v}" for k, v in kpi.items() if k != "kpi_main")
        try:
            from routes.brain_rag import recall_negative_lessons
            for r in (recall_negative_lessons(q, k=_NEGATIVE_RECALL_K) or []):
                _add("refuted", r.get("text", ""))
        except Exception as e:
            logger.debug("lane-driver negative recall failed: %s", e)
        for r in (retrieve_lessons(q, k=4) or []):
            _add("lesson", r.get("text", ""))
        for r in (retrieve_context(q, k=3, corpus="brain_findings") or []):
            _add("finding", r.get("text", ""))
    except Exception as e:
        logger.debug("lane-driver recall failed: %s", e)
    return out[:_RECALL_CAP]


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
{effector_block}
CONTEXT — current cycle KPI table (all lanes):
{kpi_table}

Platform facts: north-star = distinct external AI agents/week; conversions close via human-in-the-loop; citation velocity is the authority north-star; Bing recovery in progress after the June slug-churn purge (IndexNow resubmitted 07-04/05); the URL-elicitation experiment owns in-session conversion."""

_USER_TMPL = """LANE THIS CYCLE: {lane}

Current KPIs:
{kpi_json}

Recalled lessons and findings (best first; [refuted] entries are claims the verifier REFUTED or the owner RETRACTED, proposals rejected as duplicates, and fixes that FAILED — do NOT repeat them; then your own past decisions/outcomes — weigh regressed/flat outcomes heavily before repeating an action):
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

    # ★lane 3: the action enum is built from the CURRENT action space, which
    # includes granted registry effectors. A module-scope enum could only ever
    # offer the hardcoded eight.
    _acts, _acts_basis = available_actions()
    # an enum option the charter never describes is half-wired — the
    # model can select it but was told nothing about what it does.
    _effector_lines = []
    for _k in sorted(_acts):
        if not _k.startswith(_EFFECTOR_PREFIX):
            continue
        _cls = _k[len(_EFFECTOR_PREFIX):]
        _effector_lines.append(
            f"- {_k}: run ONE queued row of the granted, reversible action "
            f"class `{_cls}` through the registry's verified drain "
            f"(pre-read → claim → mutate → post-read → verdict). Only offered "
            f"while the class is granted and its breaker is clear.")
    effector_block = ("\n" + "\n".join(_effector_lines) + "\n"
                      if _effector_lines else "")
    system = _CHARTER.format(kpi_table=kpi_table, effector_block=effector_block)
    user = _USER_TMPL.format(
        lane=lane,
        kpi_json=json.dumps({k: v for k, v in kpi.items() if k != "kpi_main"}, sort_keys=True),
        recall_block="\n".join(f"- [{r['src']}] {r['text']}" for r in recall) or "(none recalled)",
        prev_block=json.dumps(prev, sort_keys=True, default=str) if prev else "(first decision on this lane)",
    )
    body, applied = build_messages_body(model, system, [{"role": "user", "content": user}],
                                        max_tokens=16000,
                                        schema=decision_schema(_acts))
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
    actions, actions_basis = available_actions()
    if action not in actions:
        return {"dispatched": False,
                "note": f"unknown action {action!r} → treated as stop",
                "action_space": actions_basis}
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
    # ★lane 3: registry effector. Delegated to squasher_action_classes so this
    # path inherits the drain's guards rather than duplicating them.
    if action.startswith(_EFFECTOR_PREFIX):
        cls = action[len(_EFFECTOR_PREFIX):]
        try:
            from routes import squasher_action_classes as _ac
        except Exception as e:
            return {"dispatched": False, "note": f"registry import failed: {str(e)[:100]}"}
        try:
            with _ac._conn() as conn, conn.cursor() as cur:
                cls_row = _ac.class_row(cur, cls)
                # Re-check eligibility at RUN time. The class was eligible when
                # the action space was built; a breaker can trip or a grant be
                # revoked between then and now, and the registry's own contract
                # is that a row edited straight into the table gets no free pass.
                ok, why = _ac.eligible(cls_row)
                if not ok:
                    return {"dispatched": False,
                            "note": f"effector {cls} not eligible at run time: {why}"}
                row = _ac.oldest_open_row_of_class(cur, cls)
                if row is None:
                    # Nothing to act on is not a failure, and must not be
                    # recorded as one — that conflation is what this whole
                    # shell is about.
                    return {"dispatched": False,
                            "note": f"effector {cls}: no open row awaiting_ops"}
                res = _ac.execute_one(conn, cur, row, cls_row)
                try:
                    conn.commit()
                except Exception:
                    pass
            return {"dispatched": bool(res.get("executed")),
                    "note": f"effector {cls} → {res.get('outcome')}",
                    "effector": res}
        except Exception as e:
            return {"dispatched": False,
                    "note": f"effector {cls} failed: {str(e)[:120]}"}
    # endpoint dispatch
    path = actions[action]
    if path is None:
        return {"dispatched": False, "note": f"action {action!r} has no dispatch path"}
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
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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

    # ★2026-08-29: the action space, READABLE FROM OUTSIDE THE PROCESS.
    #
    # #3322 gated registry effectors behind BRAIN_LANE_DRIVER_EFFECTORS, and
    # the only way to confirm the gate was to read the source or trust the
    # unit tests — this endpoint returned `decisions` and `ok` and nothing
    # else. "No effector has been dispatched" was the closest thing to
    # evidence available, and that is absence of evidence: the driver mostly
    # chooses `stop` anyway, so the observation is the same whether the gate
    # works or not.
    #
    # A shell whose entire subject is that decisions must be legible from
    # outside cannot leave its own action space unreadable. This publishes
    # WHAT the driver may choose and WHY the registry contributed nothing —
    # opted out / globally killed / unreadable are three different facts and
    # each reports as itself.
    #
    # Never fatal: a failure here degrades the field, never the endpoint. The
    # decisions list is what an operator came for.
    try:
        acts, basis = available_actions()
        space = {
            "verbs": sorted(acts.keys()),
            "count": len(acts),
            "static": basis.get("static", []),
            "registry": basis.get("registry", []),
            "registry_state": basis.get("registry_state"),
            "effectors_opted_in": effectors_opted_in(),
            "act_disabled": _act_disabled(),
        }
    except Exception as e:  # noqa: BLE001
        space = {"known": False, "error": str(e)[:200]}
    return jsonify(ok=True, decisions=rows, action_space=space), 200
