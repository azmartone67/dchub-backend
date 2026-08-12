"""
Brain L8 — Orchestrator (2026-05-19).

Reads ALL brain layer outputs (L0 findings + L3 memory + L6 predictions
+ L7 proposals) + funnel data + last-24h commits, calls
Claude ONCE, and returns a prioritized action plan with confidence
scores. Replaces the human end-of-session "what to do tonight"
synthesis with an automated 6h cron.

What makes L8 different from L2 (narrative):
  L2 = 3-paragraph prose digest
  L8 = structured action plan with priorities + effort + confidence

Endpoints:
  GET  /api/v1/brain/orchestrator         cached plan (5min TTL)
  POST /api/v1/brain/orchestrator/refresh forces recompute (admin)
"""

import os
import json
import time
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request
from utils.anthropic_helper import anthropic_messages_url
from routes.brain_llm_spend import instrumented_post as _llm_post

logger = logging.getLogger(__name__)
brain_layer8_bp = Blueprint("brain_layer8", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

_CACHE = {"plan": None, "computed_at": 0.0}
_TTL = 300


# ── COST SAFETY (2026-06-19) ─────────────────────────────────────────
# This endpoint is fired by a ~4x/day cron and, with ANTHROPIC_API_KEY set,
# would otherwise call Claude on every hit with NO off-switch and NO daily cap.
# Mirror the brain_self_director EXEMPLAR: a _truthy/_enabled flag that ships
# DARK (default OFF) plus a SERVER-SIDE daily cap, both enforced BEFORE any
# model call in cost-first guard order. Both are ADDITIVE: when the flag is ON
# (operator opt-in) the endpoint behaves exactly as before.
_ORCH_FLAG = "BRAIN_ORCHESTRATOR_ENABLED"
_ORCH_CAP_ENV = "BRAIN_ORCHESTRATOR_DAILY_CAP"

# Module-level in-memory daily counter (UTC-date keyed). The orchestrator has
# no natural per-row output table (its output is the in-memory _CACHE plan), so
# per the brief a best-effort in-memory counter is the backstop here — the FLAG
# is the primary control. {"date": "YYYY-MM-DD", "count": N}.
_DAILY = {"date": None, "count": 0}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """The orchestrator's autonomous Claude refresh ships DARK. Default OFF so a
    cron-fired loop can't burn API budget until an operator flips the flag. When
    off, /refresh is a NO-OP that makes ZERO model calls."""
    # BRAIN_HOST gate (2026-06-27): when the brain runs on a dedicated worker, the
    # web replicas carry BRAIN_HOST=false and must NOT execute the ~56s Claude
    # orchestrator call even if the external cron still POSTs here — that call is
    # exactly what we moved off the request-serving process. Defaults 'true' so
    # single-service deploys are unchanged.
    if os.environ.get("BRAIN_HOST", "true").strip().lower() != "true":
        return False
    return _truthy(os.environ.get(_ORCH_FLAG))


def _daily_cap() -> int:
    """Server-side daily cap on orchestrator refreshes that reach a model — the
    cost ceiling. Default 4 (the cron fires ~4x/day). Enforced in the handler,
    NOT trusted to the cron schedule."""
    try:
        return max(0, int(os.environ.get(_ORCH_CAP_ENV, "4")))
    except Exception:
        return 4


def _utc_today() -> str:
    try:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        # Fail CLOSED: an unknown date string forces a roll, but if even that
        # blows up the counter below will fail closed too.
        return "ERR"


def _today_count() -> int:
    """Number of model-bound refreshes dispatched TODAY (UTC). Mirrors the
    exemplar's _today_count contract: returns a LARGE number on ANY error so a
    broken counter fails CLOSED (skips the model call) rather than letting the
    loop run uncapped. The cost ceiling must hold even when state is weird."""
    try:
        today = _utc_today()
        if today == "ERR":
            return 10 ** 9
        if _DAILY.get("date") != today:
            # New UTC day -> reset the window.
            _DAILY["date"] = today
            _DAILY["count"] = 0
        return int(_DAILY.get("count") or 0)
    except Exception as e:
        logger.warning(f"L8 orchestrator: today_count failed: {e}")
        return 10 ** 9


def _bump_today_count() -> None:
    """Record that ONE model-bound refresh was dispatched today. Best-effort;
    never raises. Called right before the Claude call so the cap counts actual
    model dispatches, not skipped/dark ticks."""
    try:
        today = _utc_today()
        if _DAILY.get("date") != today:
            _DAILY["date"] = today
            _DAILY["count"] = 0
        _DAILY["count"] = int(_DAILY.get("count") or 0) + 1
    except Exception as e:
        logger.warning(f"L8 orchestrator: bump_today_count failed: {e}")


def _internal(path: str, timeout: int = 3) -> dict:
    """r33-Q+l8-self-call-fix (2026-05-21): default timeout slashed
    from 8s → 3s. _gather_context() does 9-10 of these calls to itself
    via HTTP — when localhost:8080 is busy, each call sat for up to 8s,
    blowing the total budget past 60s and forcing L8 in-flight to 90+s.
    With 3s caps, total worst case is 30s for all calls combined; the
    Claude call gets the remaining budget without flapping watchdog.
    Tuple form (connect, read) so a single slow chunk can't extend
    indefinitely.

    ★2026-08-12 — body moved to util/internal_fetch. The tuple timeout is
    PRESERVED and passed straight through: probe() forwards `timeout` to
    requests untouched, so the 1s-connect/3s-read budget this docstring
    argues for still holds. What changed is only that a timeout no longer
    looks identical to an honest empty payload."""
    from util.internal_fetch import data_of, probe
    return data_of(probe(path, (1, timeout)))


def _gather_context() -> dict:
    """Pull every brain layer output + funnel + commits into one dict.

    ★Outreach is deliberately NOT among them — see the note at the removed
    probe below. The docstring used to claim it, which is how a permanently
    403'd input passed for a working one."""
    # r33-Q+l8-self-call-fix (2026-05-21): consistency-radar timeout
    # SLASHED 25s → 5s. When the radar itself is slow (which is the
    # very symptom L8 is trying to summarize), a 25s wait here cascades:
    # gather_context blocks 25s, then Claude call blocks 25s+, then
    # watchdog flaps. 5s is enough for a healthy radar response (~1s
    # typical) and short enough to fail fast when the radar is broken
    # (then L8 just summarizes with empty findings, which is honest).
    findings = (_internal("/api/v1/brain/consistency-radar", 5).get("findings") or [])
    memory   = _internal("/api/v1/brain/memory/stats")
    predict  = _internal("/api/v1/brain/predictions")
    proposed = _internal("/api/v1/brain/proposed-detectors")
    funnel   = _internal("/api/v1/mcp/funnel")
    ws       = _internal("/api/v1/marketing/worker-status")
    # ★2026-08-12 — outreach probe REMOVED. /api/v1/media/outreach-log is
    # admin-gated ("admin only — media outreach log contains recipient PII")
    # and this loopback call sent no key, so it has returned 403 for its entire
    # life. The bare-{} swallow made that look like "no outreach activity", and
    # L8 has been reporting fabricated Nones as outreach state ever since. The
    # envelope (util/internal_fetch) surfaced it on 2026-08-12.
    # Owner decision, same date: DROP the probe rather than send X-Internal-Key,
    # because the fix would pipe recipient PII into the Claude prompt on every
    # tick and into the brain_llm_spend token ledger. The key is kept in the
    # context as an explicit "not read" marker — a missing key would read as an
    # oversight, and the model would infer zero from silence, which is the exact
    # failure this whole series exists to end.
    # Phase FF+7 (2026-05-19): include L14 causal chains. L14 finds
    # root-cause groupings across layers — feeding its output into L8's
    # prompt gives the orchestrator a head-start on prioritization
    # ("don't list 46 findings — fix the 2 root causes that produce them").
    causal   = _internal("/api/v1/brain/causal", 6)
    # Phase FF+7 (2026-05-19): also include L11 QA results so L8 knows
    # which surfaces are currently failing/slow without re-probing.
    qa       = _internal("/api/v1/brain/qa-agent", 6)
    # Phase FF+7 (2026-05-19): redeem funnel for actual stage-level leak data
    redeem   = _internal("/api/v1/redeem/funnel-stats", 6)
    # Recent commits via GitHub API
    commits = []
    try:
        import requests
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
        since = (_dt.datetime.utcnow() - _dt.timedelta(hours=24)).isoformat() + "Z"
        h = {"Accept": "application/vnd.github+json"}
        if token: h["Authorization"] = f"Bearer {token}"
        r = requests.get(f"https://api.github.com/repos/{repo}/commits",
                         params={"since": since, "per_page": 30},
                         headers=h, timeout=10)
        if r.status_code == 200:
            commits = [(c.get("commit", {}).get("message") or "").split("\n")[0]
                       for c in (r.json() or [])][:20]
    except Exception: pass

    return {
        "findings_count":     len(findings),
        "findings_by_issue":  _by_issue(findings),
        "top_findings":       [{"issue": f.get("issue"),
                                "url":   f.get("url"),
                                "detail": (f.get("detail") or "")[:160]}
                               for f in findings[:8]],
        "memory_records":     memory.get("total_records", 0),
        "top_recurring":      memory.get("top_recurring_issues", [])[:5],
        "predictions":        (predict.get("predictions") or [])[:5],
        "proposed_detectors_pending": [p for p in (proposed.get("proposals") or [])
                                        if p.get("decision") == "pending"],
        "funnel": {
            # Honest-numbers fence: feed the orchestrator the DE-LOOPED
            # real-external count (tool_calls_7d_real) so its synthesis doesn't
            # headline the gross incl-loops number (~35-41k/wk of our own
            # selfheal/probe/sweep loop). Falls back to gross if _real absent.
            "tool_calls_7d":      funnel.get("tool_calls_7d_real")
                                  or funnel.get("tool_calls_7d"),
            "tool_calls_7d_incl_loops": funnel.get("tool_calls_7d"),
            "upgrade_signals_7d": funnel.get("upgrade_signals_7d"),
            "conversions_30d":    funnel.get("conversions_30d"),
            "keys_by_tier":       funnel.get("keys_by_tier"),
            "addressable_paid_demand": (funnel.get("paid_tool_demand_30d") or [])[:3],
        },
        "publisher": {
            "status":       (ws.get("distribution") or {}).get("status"),
            "queued":       (ws.get("distribution") or {}).get("queued_unpublished"),
            "published_7d": (ws.get("distribution") or {}).get("published_7d"),
        },
        "outreach": {
            "read": False,
            "note": ("NOT READ — /api/v1/media/outreach-log is admin-gated "
                     "(recipient PII) and this layer deliberately holds no "
                     "credential for it. Absence of outreach data here is a "
                     "DELIBERATE GAP, not a measurement of zero outreach. Do "
                     "not infer anything about outreach volume or reply rate."),
        },
        # Phase FF+7: L14 causal chains — pre-grouped root causes that
        # L8 should use as its starting point rather than re-deriving.
        "causal_chains": (causal.get("analysis") or {}).get("causal_chains", []),
        "causal_highest_leverage": (causal.get("analysis") or {}).get("single_highest_leverage"),
        # Phase FF+7: L11 QA — current surface health snapshot.
        "qa_verdict":   qa.get("verdict"),
        "qa_errors":    (qa.get("errors") or [])[:5],
        "qa_slow":      (qa.get("slow_pages") or [])[:5],
        # Phase FF+7: redeem funnel — actual stage-level numbers, not
        # just aggregate "0 conversions". Shows WHERE in the funnel
        # the leak is.
        "redeem_funnel": {
            "paywall_hit": (redeem.get("funnel_counts") or {}).get("paywall_hit"),
            "click":       (redeem.get("funnel_counts") or {}).get("click"),
            "view":        (redeem.get("funnel_counts") or {}).get("view"),
            "submit":      (redeem.get("funnel_counts") or {}).get("submit"),
            "upgrade":     (redeem.get("funnel_counts") or {}).get("upgrade"),
            "biggest_leak": redeem.get("biggest_leak"),
        },
        "recent_commits": commits,
    }


def _by_issue(findings: list[dict]) -> dict:
    by = {}
    for f in findings:
        k = f.get("issue", "?")
        by[k] = by.get(k, 0) + 1
    return dict(sorted(by.items(), key=lambda x: -x[1])[:10])


def _build_prompt(ctx: dict) -> str:
    return f"""You are the DC Hub brain Orchestrator (L8). You see EVERYTHING:
detector findings, memory of past fixes, velocity predictions, proposed
new detectors, funnel state, publisher state, journalist outreach log,
last 24h of git commits, L14's pre-computed CAUSAL CHAINS (cross-layer
root-cause groupings), L11 QA verdict + current errors/slow pages,
and the REAL redeem-funnel stage counts (paywall_hit -> click -> view
-> submit -> upgrade).

Synthesize one prioritized action plan for the founder.

Key heuristic: if `causal_chains` in the context is non-empty, those are
already-grouped root causes (L14 ran cross-layer analysis). Use them
as your starting point — don't re-derive the same conclusions from
individual findings. The `causal_highest_leverage` field names the one
chain L14 marked as highest impact. Promote that to action #1 unless
you have strong reason otherwise.

Context:
{json.dumps(ctx, indent=2, default=str)[:8000]}

Return a JSON object (NO markdown fences) with this exact shape:
{{
  "summary": "<one-paragraph state-of-the-system, ≤3 sentences>",
  "actions": [
    {{
      "title":      "<5-10 word action title>",
      "rationale":  "<1-2 sentence why-this-matters>",
      "effort":     "30s | 5min | 30min | 2h | 1day",
      "confidence": "high | medium | low",
      "priority":   1,
      "category":   "revenue | reliability | distribution | brain | infrastructure",
      "specific_command": "<exact shell command OR null if multi-step>"
    }}
  ],
  "watch_next_24h": [
    "<one short-line metric or signal to monitor>"
  ],
  "celebration":   "<one positive callout — what's working that shouldn't be ignored>"
}}

Order actions by priority (1 = do first). Cap at 5 actions. The founder
has finite attention; ruthless prioritization beats completeness.
Reply with ONLY the JSON object, no preamble."""


def _call_claude(prompt: str) -> dict | None:
    if not _ANTHROPIC_KEY: return None
    try:
        import requests
        # r33-Q+l8-timeout (2026-05-21): tightened from 45s to (10, 25)
        # tuple = 10s connect, 25s read. Tonight's logs showed L8 calls
        # held in-flight 90-110s. The old timeout=45 was a single value
        # which requests interprets as BOTH connect+read separately, so
        # the real cap was 90s (worst case). The tuple form makes the
        # cap a hard 25s read. When Anthropic is slow, L8 returns None
        # fast and the watchdog stops counting healthcheck failures.
        # Cap is short enough to fail fast, long enough for an honest
        # Sonnet 4.5 response (typical 5-15s).
        r = _llm_post("brain_layer8_orchestrator",
            anthropic_messages_url(),
            headers={"x-api-key": _ANTHROPIC_KEY,
                     "User-Agent": "dchub-brain/1.0",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-5",
                  "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=(10, 25),
        )
        if r.status_code != 200:
            logger.warning(f"L8 Claude {r.status_code}: {r.text[:200]}")
            return None
        body = r.json() or {}
        text = "".join(b.get("text","") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"): text = text[4:].lstrip("\n")
        return json.loads(text)
    except Exception as e:
        logger.warning(f"L8 Claude call failed: {e}")
        return None


@brain_layer8_bp.route("/api/v1/brain/orchestrator", methods=["GET"])
def orchestrator():
    """Cached action plan. 5min TTL — recompute via /refresh."""
    now = time.monotonic()
    if _CACHE["plan"] and (now - _CACHE["computed_at"]) < _TTL:
        return jsonify(
            ok=True,
            plan=_CACHE["plan"],
            cached=True,
            cache_age_seconds=int(now - _CACHE["computed_at"]),
        ), 200
    return jsonify(
        ok=False,
        plan=None,
        note="Plan not yet computed. POST /api/v1/brain/orchestrator/refresh (admin) or wait for 6h cron.",
    ), 200


@brain_layer8_bp.route("/api/v1/brain/orchestrator/refresh",
                          methods=["POST", "GET"])
def orchestrator_refresh():
    """Phase FF+7-durability (2026-05-19): refactored to fire-and-
    forget. Returns 202 immediately + spawns a background thread for
    the Claude call. Result writes to _CACHE when done. Caller reads
    via GET /api/v1/brain/orchestrator (cached). No more synchronous
    60s holds on gunicorn workers — that pattern is what crashed the
    container 4 times today.

    Brain L20 (durability guard) is consulted BEFORE spawning the
    thread. If RSS is approaching the watchdog threshold OR too many
    Claude calls have started recently, this endpoint refuses and
    returns 429.

    COST-SAFETY GUARD ORDER (2026-06-19, cost-first — every guard before the
    model call short-circuits WITHOUT dispatching Claude):
      1. flag off    -> 200 {ok:false, skipped:disabled}   (NO model call;
                        ships DARK behind BRAIN_ORCHESTRATOR_ENABLED, default OFF)
      2. no api key  -> 503 (NO model call; degrade gracefully)
      3. daily cap   -> 200 {ok:false, skipped:daily_cap}  (NO model call; the
                        cost ceiling, BRAIN_ORCHESTRATOR_DAILY_CAP default 4)
      4. L20 throttle-> 429 (NO model call; existing durability guard). If L20
                        is UNAVAILABLE we now FAIL CLOSED (429) instead of
                        silently proceeding unguarded.
      5. else        -> spawn the ONE background Claude refresh (unchanged).
    """
    # ── Guard 0: admin (unchanged) ───────────────────────────────────
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if request.method == "POST" and _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    # ── Guard 1: flag off → NO model call (ships DARK) ───────────────
    if not _enabled():
        return jsonify(
            ok=False, ran=False, skipped_reason="disabled",
            note=("Orchestrator autonomous refresh ships DARK. Set "
                  f"{_ORCH_FLAG}=1 to enable (operator opt-in)."),
        ), 200

    # ── Guard 2: no API key → NO model call (degrade gracefully) ─────
    if not _ANTHROPIC_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set"), 503

    # ── Guard 3: daily cap → NO model call (the cost ceiling) ────────
    cap = _daily_cap()
    try:
        used = _today_count()
    except Exception:
        # Fail CLOSED on a broken counter — don't run uncapped.
        used = 10 ** 9
    if used >= cap:
        return jsonify(
            ok=False, ran=False, skipped_reason="daily_cap",
            used_today=used, daily_cap=cap,
            note=("Orchestrator daily cap reached; no model call. Raise "
                  f"{_ORCH_CAP_ENV} to allow more refreshes/day."),
        ), 200

    # ── Guard 4: L20 durability guard. FAIL CLOSED if L20 unavailable ─
    # Previously this fell back to no-op lambdas and proceeded UNGUARDED if the
    # import failed; now an unavailable durability guard refuses the refresh
    # (429) so a missing guard can't let an uncapped Claude call through.
    try:
        from routes.brain_layer20_durability import (
            can_start_claude_call, register_claude_call_start,
            register_claude_call_end)
    except Exception as _e:
        logger.warning(f"L8 orchestrator: L20 durability guard unavailable, "
                       f"failing closed (no model call): {_e}")
        return jsonify(ok=False, throttled=True,
                       reason="durability_guard_unavailable",
                       cached_plan_age_seconds=(
                           int(time.monotonic() - _CACHE["computed_at"])
                           if _CACHE.get("computed_at") else None)), 429
    try:
        allowed, reason = can_start_claude_call("L8")
        if not allowed:
            return jsonify(ok=False, throttled=True, reason=reason,
                           cached_plan_age_seconds=(
                               int(time.monotonic() - _CACHE["computed_at"])
                               if _CACHE.get("computed_at") else None)), 429
    except Exception as _e:
        # The guard itself errored — fail CLOSED rather than proceed unguarded.
        logger.warning(f"L8 orchestrator: L20 can_start check errored, "
                       f"failing closed (no model call): {_e}")
        return jsonify(ok=False, throttled=True,
                       reason="durability_guard_error",
                       cached_plan_age_seconds=(
                           int(time.monotonic() - _CACHE["computed_at"])
                           if _CACHE.get("computed_at") else None)), 429

    # The model call is now committed (a background thread is about to dispatch
    # Claude). Count it against today's cap up front so concurrent/rapid cron
    # hits can't all slip under the cap before any of them increments.
    _bump_today_count()

    def _bg_refresh():
        call_id = None
        try:
            try:
                from routes.brain_layer20_durability import register_claude_call_start, register_claude_call_end
                call_id = register_claude_call_start("L8")
            except Exception: pass
            ctx = _gather_context()
            prompt = _build_prompt(ctx)
            plan = _call_claude(prompt)
            if plan:
                _CACHE["plan"] = plan
                _CACHE["computed_at"] = time.monotonic()
                _CACHE["based_on"] = {
                    "findings_count":   ctx["findings_count"],
                    "memory_records":   ctx["memory_records"],
                    "predictions":      len(ctx["predictions"]),
                    "proposed_detectors_pending": len(ctx["proposed_detectors_pending"]),
                    "recent_commits":   len(ctx["recent_commits"]),
                    "outreach_sent":    (ctx["outreach"] or {}).get("total_sent"),
                }
                # Loop 2 (r70, 2026-06-03): stop the plan dead-ending. Surface the
                # TOP recommendation into the brain DECISION FEED (brain_notifications)
                # so a human — or a downstream layer — sees and acts on it, and
                # /brain/evolution counts it. We deliberately do NOT auto-execute:
                # L8 emits free-form advice + an arbitrary `specific_command` shell
                # string, which must never run unattended. This is the SAFE half of
                # plan→act (visibility); true autonomous execution needs L8 to emit a
                # structured, whitelisted action vocabulary first (scoped follow-up).
                try:
                    _acts = plan.get("actions") or []
                    if _acts:
                        _top = _acts[0] or {}
                        from routes.brain_evolution import log_notification as _logn
                        _logn(
                            "orchestrator_plan",
                            f"L8 top priority: {str(_top.get('title') or '(untitled)')[:120]}",
                            detail={
                                "rationale":  str(_top.get("rationale") or "")[:300],
                                "category":   _top.get("category"),
                                "effort":     _top.get("effort"),
                                "confidence": _top.get("confidence"),
                                "summary":    str(plan.get("summary") or "")[:300],
                                "execution":  "human_gated (L8 emits shell; never auto-run)",
                            },
                            url="https://dchub.cloud/brain",
                            severity="info",
                        )
                except Exception as _e:
                    logger.warning(f"L8 plan→decision-feed log failed: {_e}")
                logger.info("L8 orchestrator/refresh background call complete")
            else:
                logger.warning("L8 orchestrator/refresh background call: no plan returned")
        except Exception as e:
            logger.warning(f"L8 orchestrator/refresh background error: {e}")
        finally:
            if call_id is not None:
                try:
                    from routes.brain_layer20_durability import register_claude_call_end
                    register_claude_call_end(call_id)
                except Exception: pass

    import threading as _th
    _th.Thread(target=_bg_refresh, daemon=True, name="l8-orchestrator-refresh").start()
    return jsonify(
        ok=True,
        accepted=True,
        note=("Refresh started in background. Poll GET "
              "/api/v1/brain/orchestrator for the updated plan in ~30-90s."),
        previous_plan_age_seconds=(int(time.monotonic() - _CACHE["computed_at"])
                                     if _CACHE.get("computed_at") else None),
    ), 202
