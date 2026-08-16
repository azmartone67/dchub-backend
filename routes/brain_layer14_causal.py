"""
Brain L14 — Causal Reasoner (2026-05-18).

L1-L9 each find symptoms in isolation. L8 prioritizes them. L14 ties
them together: when two layers flag related findings, L14 calls Claude
with the JOINED context and asks for the ROOT CAUSE plus a smallest-safe
fix.

Example causal chains L14 can recognize (input → reasoning → output):

  funnel.upgrade_signals_7d ↑ + funnel.conversions_30d flat
  → "paywall fires but users don't redeem"
  → root cause: redeem page perf OR email-capture friction OR pricing-page CTA
  → action: probe each candidate, propose A/B

  qa.iso_landing failing 3+ runs + auto-fix.iso_endpoint_unreachable
  → "auto-fix layer is firing but not curing"
  → root cause: the fix recipe (trigger workflow) doesn't address the
     actual failure (upstream ISO API down? CORS?)
  → action: read failure body, propose new recipe

  freshness.facilities >SLA + scheduler.facility_discovery runs=0
  → "ingestion job dead, not stale-data problem"
  → root cause: scheduler thread crashed OR admin key mismatch OR
     upstream API broken
  → action: check thread alive + admin key match + upstream

L14 is admin-gated (it can propose PRs); read-only candidates view is
public.

Endpoints:
  GET  /api/v1/brain/causal           — latest causal analysis (cached 1h)
  POST /api/v1/brain/causal/analyze   — admin: re-run with Claude
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
brain_layer14_bp = Blueprint("brain_layer14", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
# 2026-08-15: the module-level _ADMIN_KEY snapshot is GONE on purpose.
# It was an import-time read, so a process that started before the env
# was set held "" forever, and the gate that tested `and _ADMIN_KEY`
# silently disabled itself. Auth now goes through
# internal_auth.require_internal_or_admin, which reads os.environ at
# request time. Do not reintroduce this name.

_CACHE = {"analysis": None, "computed_at": 0.0}
_TTL = 3600  # 1h — causal analyses are expensive, change slowly
_DB_TTL = 86400  # 24h — DB-persisted analysis is the cross-worker source of truth


# ── COST SAFETY (2026-06-19): ships DARK behind a default-OFF flag + a
# server-side daily cap, mirroring routes/brain_self_director.py. /analyze is
# cron-fired ~4x/day and, with ANTHROPIC_API_KEY set, fired a GUARANTEED model
# call every time with NO off-switch and NO daily cap. The guards below run
# cost-first BEFORE any model work is spawned: flag off -> no key -> daily cap
# -> then (and only then) the existing L20 durability guard + the background
# Claude call. EVERY change here is ADDITIVE; the flag DEFAULTS OFF (operator
# flips it later). When the flag IS on, the endpoint behaves exactly as before.
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """L14 causal/analyze ships DARK. Default OFF so a cron-fired loop can't burn
    API budget until an operator flips BRAIN_CAUSAL_ENABLED. When off, /analyze is
    a NO-OP that makes ZERO model calls (the background thread is never spawned)."""
    return _truthy(os.environ.get("BRAIN_CAUSAL_ENABLED"))


def _daily_cap() -> int:
    """Server-side daily cap on causal analyses — the cost ceiling. Default 4.
    Enforced in the handler, NOT trusted to the cron schedule."""
    try:
        return max(0, int(os.environ.get("BRAIN_CAUSAL_DAILY_CAP", "4")))
    except Exception:
        return 4


# In-memory UTC-date-keyed daily counter. The natural output table
# (brain_causal_cache) is a single-row upsert (id=1) so it cannot count "today's
# rows" — the FLAG is the primary control, and this best-effort backstop bounds
# cost per worker. Mirrors _today_count's fail-CLOSED contract: any error counting
# returns a huge number so a broken counter never lets the loop run uncapped.
_DAILY = {"date": None, "count": 0}


def _utc_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _today_count() -> int:
    """Causal analyses STARTED today (UTC). Returns a LARGE number on any error so
    a broken counter fails CLOSED (skips the model call) rather than running
    uncapped — the cost ceiling must hold even when something is off."""
    try:
        if _DAILY.get("date") != _utc_today():
            _DAILY["date"] = _utc_today()
            _DAILY["count"] = 0
        return int(_DAILY.get("count") or 0)
    except Exception as e:
        logger.warning(f"L14 _today_count failed: {e}")
        return 10 ** 9


def _bump_today_count() -> None:
    """Record that one causal analysis was STARTED today. Best-effort; counted
    BEFORE the model call so a crash mid-call still consumes a slot (fail-safe
    toward UNDER-running, never over)."""
    try:
        if _DAILY.get("date") != _utc_today():
            _DAILY["date"] = _utc_today()
            _DAILY["count"] = 0
        _DAILY["count"] = int(_DAILY.get("count") or 0) + 1
    except Exception as e:
        logger.warning(f"L14 _bump_today_count failed: {e}")


def _db():
    """Postgres/Neon connection or None. r89e (2026-06-15): the causal analysis
    MUST persist to the DB, not just the per-process _CACHE. analyze() runs in a
    daemon thread in ONE gunicorn worker while GET /causal (and L16's prediction
    capture) hit ANOTHER worker → the in-memory cache misses cross-worker (and is
    wiped on every restart). That is why brain_predictions_log stayed empty: L16
    read an always-cold cache, captured 0 chains, logged 0 predictions."""
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _persist_analysis(analysis: dict) -> None:
    """Upsert the latest causal analysis to a single-row table so every worker
    + L16 see it. Best-effort, never raises."""
    import json as _json
    if not analysis:
        return
    c = _db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS brain_causal_cache ("
                        "id INT PRIMARY KEY, analysis JSONB, "
                        "computed_at TIMESTAMPTZ DEFAULT NOW())")
            cur.execute("INSERT INTO brain_causal_cache (id, analysis, computed_at) "
                        "VALUES (1, %s, NOW()) ON CONFLICT (id) DO UPDATE "
                        "SET analysis = EXCLUDED.analysis, computed_at = NOW()",
                        (_json.dumps(analysis),))
    except Exception as e:
        logger.warning(f"L14 _persist_analysis failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


def _load_analysis():
    """Load the latest DB-persisted analysis (dict) + age-seconds, or (None, None).
    The cross-worker source of truth behind GET /causal."""
    c = _db()
    if c is None:
        return None, None
    try:
        with c.cursor() as cur:
            cur.execute("SELECT analysis, EXTRACT(EPOCH FROM (NOW()-computed_at))::int "
                        "FROM brain_causal_cache WHERE id = 1")
            row = cur.fetchone()
            if row and row[0]:
                return row[0], int(row[1] or 0)
    except Exception as e:
        logger.warning(f"L14 _load_analysis failed: {e}")
    finally:
        try: c.close()
        except Exception: pass
    return None, None


# ★2026-08-11 — the bare-{} swallow that used to live here is gone. It made a
# timeout, a 500 and a healthy-but-empty payload identical downstream, and 17 of
# the brain's 20 live L18 lessons are that ambiguity being re-learned every
# tick. The envelope lives in util/internal_fetch; see its docstring for the
# evidence. This wrapper stays for the call sites that only want the payload.
def _internal(path: str, timeout: int = 8) -> dict:
    from util.internal_fetch import data_of, probe
    return data_of(probe(path, timeout))


# The probe set L14 reasons over. Named here so context_health can report each
# one by name and the context-integrity shell can re-run exactly this set.
_CONTEXT_PROBES = (
    ("findings",     "/api/v1/brain/consistency-radar",          20),
    ("funnel",       "/api/v1/mcp/funnel",                        8),
    ("freshness",    "/api/v1/freshness/radar",                   8),
    # ★25s, not 8s. /api/v1/brain/predictions is not slow by accident — its own
    # _gather_predictions() makes THREE nested loopback calls before it computes
    # anything (mcp/funnel @5s, marketing/worker-status @5s, reach @8s = 18s of
    # budget), then runs a per-metric _velocity() query that opens its own
    # connection each time. An 8s probe timeout was shorter than the endpoint's
    # own worst case, so a perfectly healthy service COULD NOT answer in time
    # and lane 1 reported it as an instrument failure every tick. A timeout you
    # know the callee cannot meet does not measure the callee, it measures the
    # timeout. See routes/brain_layer6_predictive.py:152.
    ("predictions",  "/api/v1/brain/predictions",                25),
    ("proposed",     "/api/v1/brain/proposed-detectors",          8),
    ("qa_agent",     "/api/v1/brain/qa-agent",                    8),
    ("expansion",    "/api/v1/brain/expansion",                   8),
    ("publisher",    "/api/v1/marketing/worker-status",           8),
    # ★2026-08-12 — ("outreach", "/api/v1/media/outreach-log") REMOVED.
    # Admin-gated ("admin only — media outreach log contains recipient PII"),
    # and this probe carries no key, so it returned 403 for its entire life.
    # The bare-{} swallow rendered that as "no outreach activity". Owner
    # decision: drop it rather than send X-Internal-Key, which would put
    # recipient PII into the Claude prompt on every causal tick.
    # ★Do NOT re-add it without that decision being revisited.
    ("schedulers",   "/api/schedulers/audit",                     8),
    # L18 lessons — what the brain has distilled from its own history
    ("lessons",      "/api/v1/brain/lessons",                     8),
    # L16 calibration — how often the brain's past predictions
    # at each confidence level have been correct
    ("calibration",  "/api/v1/brain/self-critique/calibration",   8),
)

# Payload key to unwrap + cap, for the probes that carry a list under a key.
_UNWRAP = {
    "findings":    ("findings",       25),
    "predictions": ("predictions",    10),
    "proposed":    ("proposals",       5),
    "lessons":     ("active_lessons", 10),
}


def _gather_joined_context() -> dict:
    """Pull state from EVERY layer that has a signal. The point of L14
    is to look across layers — not at any one in isolation.

    Phase FF+7-L18 (2026-05-19): also pulls L18 consolidated lessons +
    L16 calibration data so each L14 analysis is informed by what the
    brain has learned about itself.

    2026-08-11: every probe now carries its own outcome, and ctx.context_health
    names the probes that could NOT be measured. Before this, a dead endpoint
    and an endpoint honestly reporting nothing both arrived as `{}` and the
    model had no way to tell them apart — so it said "cannot verify", and L18
    consolidated that into a lesson, 17 times."""
    from util.internal_fetch import health_of, probe

    envs = {name: probe(path, timeout) for name, path, timeout in _CONTEXT_PROBES}
    ctx = {}
    for name, env in envs.items():
        data = env.get("data") or {}
        if name in _UNWRAP:
            key, cap = _UNWRAP[name]
            ctx[name] = ((data.get(key) if isinstance(data, dict) else None)
                         or [])[:cap]
        else:
            ctx[name] = data
    ctx["context_health"] = health_of(envs)
    return ctx


def _build_prompt(ctx: dict) -> str:
    return f"""You are the DC Hub brain L14 — the Causal Reasoner.

L1-L9 each report SYMPTOMS in isolation. L8 prioritizes them. Your job
is to find ROOT-CAUSE CHAINS by reading across layers. Most useful
output: "symptom A in layer X plus symptom B in layer Y both stem from
single root cause Z — and here's the smallest-safe fix."

Examples of what good cross-layer reasoning looks like:
  - "upgrade_signals_7d ↑ + conversions_30d flat → paywall fires but
     redeem-page conversion is broken. Probe /api/v1/redeem/funnel-stats
     and look for the actual leak stage."
  - "freshness.facilities >SLA + schedulers.facility_discovery.runs=0
     → ingestion-job dead, not stale-data problem. The fix is in
     scheduler health, not in any data pipeline."
  - "qa.iso_landing chronic-fail + 5 auto-fix attempts all 'success'
     → recipe is firing but not curing. The fix recipe is wrong.
     Examine the actual HTTP body of the failing probe."

Anti-patterns to avoid:
  - Listing each finding separately (that's L8's job, you're going deeper)
  - Suggesting fixes for symptoms — fix root causes
  - Speculating without naming the specific signal that supports your
    claim. If you can't cite a signal, you don't know yet.

╔══════════════════════════════════════════════════════════════════╗
║ META-COGNITION (Phase FF+7-L18, 2026-05-19):                     ║
║ This prompt is INFORMED BY THE BRAIN'S OWN TRACK RECORD.         ║
║                                                                  ║
║ L18 has distilled the brain's recent episodes into NAMED LESSONS ║
║ (see ctx.lessons below). Apply them — if a current chain matches ║
║ a known pattern from past episodes, name the lesson explicitly   ║
║ and use it to set confidence appropriately.                      ║
║                                                                  ║
║ L16 has tracked the brain's calibration (see ctx.calibration).   ║
║ If past "high" confidence chains have been correct <70% of the   ║
║ time, the brain is over-confident — be more conservative on      ║
║ "high" labels this time.                                         ║
║                                                                  ║
║ Use the lessons + calibration as input to your reasoning, not    ║
║ just as observations. They're how the brain learns from itself.  ║
╚══════════════════════════════════════════════════════════════════╝

Context (JSON):
{json.dumps(ctx, indent=2, default=str)[:9500]}

Return a JSON object (NO markdown fences) with this exact shape:

{{
  "summary": "<one-paragraph state of the system, 2-3 sentences>",
  "causal_chains": [
    {{
      "title": "<short noun phrase, <60 chars>",
      "symptoms": [
        "<finding/metric from layer X that participates in this chain>",
        "<finding/metric from layer Y that participates in this chain>"
      ],
      "root_cause_hypothesis": "<one sentence: what's actually broken>",
      "confidence": "high | medium | low",
      "smallest_safe_fix": "<concrete action — a curl, a code edit at file:line, an env var to check>",
      "verification": "<how to know if the fix worked — a curl that flips status, a metric that should change>"
    }}
  ],
  "single_highest_leverage": "<title of the chain that, if fixed first, helps the most other findings>",
  "stop_doing": "<one detector or layer that's adding noise without value — or null>"
}}

Cap at 4 chains. Quality over quantity. Reply with ONLY the JSON object."""


# ★The bespoke _spend() helper that lived here is gone: every Anthropic POST
# in routes/ now goes through brain_llm_spend.instrumented_post, which
# records the same row. Keeping both would have DOUBLE-COUNTED L14 — the
# one layer with a baseline would have been the one layer with a wrong one.


def _call_claude(prompt: str) -> dict | None:
    if not _ANTHROPIC_KEY: return None
    # r-l14-fix (2026-07-04): max_tokens=2500 truncated the 4-chain JSON mid-object
    # → json.loads threw → None → nothing persisted → the issue-janitor's resolved-
    # close arm went blind. Give the output real headroom, and on ANY failure (non-200
    # model error OR unparseable body) retry ONCE on a confirmed-valid model so a
    # mispinned/retired reasoning tier can't silently zero out L14. Log the exact
    # cause + whether the response was truncated so this is never a mystery again.
    from routes.brain_models import brain_model_for
    _models = []
    try:
        _models.append(brain_model_for("reasoning"))
    except Exception:
        pass
    if "claude-sonnet-4-5" not in _models:
        _models.append("claude-sonnet-4-5")   # confirmed-valid fallback
    import requests
    for _model in _models:
        try:
            r = _llm_post("brain_layer14_causal",
                anthropic_messages_url(),
                headers={"x-api-key": _ANTHROPIC_KEY,
                         "User-Agent": "dchub-brain/1.0",
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={
                    "model": _model,
                    "max_tokens": 8000,   # was 2500 — too small for the 4-chain JSON
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )
            if r.status_code != 200:
                # The wrapper has already ledgered this failure — a non-200
                # still spent input tokens and wall-clock, and this loop
                # retries on a second model.
                logger.warning(f"L14 Claude {r.status_code} ({_model}): {r.text[:200]}")
                continue
            body = r.json() or {}
            text = "".join(b.get("text", "") for b in (body.get("content") or [])
                           if b.get("type") == "text").strip()
            if body.get("stop_reason") == "max_tokens":
                logger.warning(f"L14 Claude ({_model}) truncated at max_tokens "
                               f"(text_len={len(text)}) — raise max_tokens")
            if text.startswith("```"):
                text = text.split("```")[1] if "```" in text else text
                if text.startswith("json"): text = text[4:].lstrip("\n")
            # Lenient: slice the outermost {...} so trailing prose/whitespace can't
            # break an otherwise-complete object.
            _s, _e = text.find("{"), text.rfind("}")
            if 0 <= _s < _e:
                text = text[_s:_e + 1]
            return json.loads(text)
        except Exception as e:
            logger.warning(f"L14 Claude call failed ({_model}): {type(e).__name__}: {str(e)[:200]}")
            continue
    return None


@brain_layer14_bp.route("/api/v1/brain/causal", methods=["GET"])
def causal():
    """Cached causal analysis. 1h TTL."""
    now = time.monotonic()
    if _CACHE["analysis"] and (now - _CACHE["computed_at"]) < _TTL:
        return jsonify(
            ok=True,
            analysis=_CACHE["analysis"],
            cached=True,
            cache_age_seconds=int(now - _CACHE["computed_at"]),
        )
    # r89e (2026-06-15): cross-worker / post-restart fall-through. analyze() ran
    # in another worker's memory (or before a restart), so the in-process _CACHE
    # is cold here. Load the DB-persisted analysis — the real source of truth that
    # L16 reads to capture predictions.
    db_analysis, db_age = _load_analysis()
    if db_analysis and (db_age is None or db_age < _DB_TTL):
        _CACHE["analysis"] = db_analysis            # warm this worker's cache
        _CACHE["computed_at"] = now - (db_age or 0)
        return jsonify(
            ok=True,
            analysis=db_analysis,
            cached=True,
            cache_source="db",
            cache_age_seconds=db_age,
        )
    return jsonify(
        ok=False,
        analysis=None,
        note=("No causal analysis yet. POST /api/v1/brain/causal/analyze "
              "(admin) or wait for cron."),
    )


@brain_layer14_bp.route("/api/v1/brain/causal/analyze",
                        methods=["POST", "GET"])
def analyze():
    """Phase FF+7-durability (2026-05-19): fire-and-forget. Returns 202
    immediately + spawns background thread. Result writes to _CACHE.
    GET /api/v1/brain/causal serves the cached analysis."""
    # 2026-08-15: was `if request.method == "POST" and _ADMIN_KEY:` — fail-OPEN
    # when the process env lacks DCHUB_ADMIN_KEY, and ungated on GET while the
    # route accepts GET. See routes/brain_layer16_self_critique.py for the full
    # note. Fail-closed, request-time, every method.
    from internal_auth import require_internal_or_admin
    if not require_internal_or_admin(request):
        return jsonify(error="unauthorized"), 401

    # ── COST GUARDS (cost-first order — each short-circuits BEFORE any model
    # work is spawned, so a dark/keyless/capped call makes ZERO model calls). ──
    # Guard 1: flag OFF -> NO model call. Ships dark; operator flips
    # BRAIN_CAUSAL_ENABLED to turn the cron-fired analysis on. Behavior is
    # otherwise UNCHANGED when the flag is on.
    if not _enabled():
        return jsonify(ok=False, skipped=True, skipped_reason="disabled",
                       note=("L14 causal/analyze ships dark. Set "
                             "BRAIN_CAUSAL_ENABLED=1 to enable.")), 200

    # Guard 2: no API key -> NO model call (degrade gracefully).
    if not _ANTHROPIC_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set"), 503

    # Guard 3: daily cap -> NO model call (the cost ceiling, enforced
    # SERVER-SIDE in the handler, not trusted to the cron schedule). Counter
    # fails CLOSED on error.
    cap = _daily_cap()
    try:
        used = _today_count()
    except Exception:
        used = 10 ** 9
    if used >= cap:
        return jsonify(ok=False, skipped=True, skipped_reason="daily_cap",
                       used_today=used, daily_cap=cap,
                       note="L14 causal/analyze daily cap reached."), 200

    # L20 durability guard
    try:
        from routes.brain_layer20_durability import can_start_claude_call
        allowed, reason = can_start_claude_call("L14")
        if not allowed:
            return jsonify(ok=False, throttled=True, reason=reason,
                           cached_analysis_age_seconds=(
                               int(time.monotonic() - _CACHE["computed_at"])
                               if _CACHE.get("computed_at") else None)), 429
    except Exception as e:
        # Fail CLOSED: if the durability guard cannot run (import/call error),
        # do NOT proceed unguarded — skip this tick. A flaky import must never
        # bypass the cost/durability gate.
        return jsonify(ok=False, throttled=True,
                       reason=f"durability_guard_unavailable:{str(e)[:80]}"), 429

    def _bg_analyze():
        call_id = None
        try:
            try:
                from routes.brain_layer20_durability import register_claude_call_start, register_claude_call_end
                call_id = register_claude_call_start("L14")
            except Exception: pass
            ctx = _gather_joined_context()
            prompt = _build_prompt(ctx)
            analysis = _call_claude(prompt)
            if analysis:
                _CACHE["analysis"] = analysis
                _CACHE["computed_at"] = time.monotonic()
                _persist_analysis(analysis)  # r89e: survive cross-worker + restart
                # #49 lane 2: persist the GRAPH, not just the narrative. Until
                # now the most expensive step in the pipeline produced an
                # hour-cached story that no decision-maker could read, so
                # rank_work() kept treating five symptoms of one cause as five
                # independent pieces of work. Best-effort by construction —
                # write_chains() never raises, and a failure here must not turn
                # a successful analysis into a failed one.
                try:
                    from routes.brain_finding_edges import write_chains
                    _edges = write_chains(analysis.get("causal_chains"))
                    if _edges.get("written"):
                        logger.info("L14 persisted %s causal edge(s) across %s "
                                    "chain(s)", _edges["written"],
                                    _edges["chains"])
                    elif _edges.get("error"):
                        logger.warning("L14 edge persist: %s", _edges["error"])
                except Exception as _e:  # noqa: BLE001
                    logger.warning("L14 edge persist failed: %s", str(_e)[:160])
                logger.info("L14 causal/analyze background call complete")
            else:
                logger.warning("L14 causal/analyze background call: no analysis returned")
        except Exception as e:
            logger.warning(f"L14 causal/analyze background error: {e}")
        finally:
            if call_id is not None:
                try:
                    from routes.brain_layer20_durability import register_claude_call_end
                    register_claude_call_end(call_id)
                except Exception: pass

    # Consume one daily slot BEFORE spawning the model work — count the START so a
    # crash mid-call still spends the slot (fail toward UNDER-running, never over).
    _bump_today_count()
    import threading as _th
    _th.Thread(target=_bg_analyze, daemon=True, name="l14-causal-analyze").start()
    return jsonify(
        ok=True,
        accepted=True,
        note=("Analysis started in background. Poll GET "
              "/api/v1/brain/causal for the updated chains in ~30-60s."),
        previous_analysis_age_seconds=(int(time.monotonic() - _CACHE["computed_at"])
                                         if _CACHE.get("computed_at") else None),
    ), 202

    # Legacy synchronous path below — unreachable but kept for diff clarity.
    # (Will be removed once durability is verified live.)
    ctx = _gather_joined_context()
    prompt = _build_prompt(ctx)
    analysis = _call_claude(prompt)
    if not analysis:
        return jsonify(ok=False, error="Claude call failed"), 503

    _CACHE["analysis"] = analysis
    _CACHE["computed_at"] = time.monotonic()

    return jsonify(
        ok=True,
        analysis=analysis,
        based_on={
            "findings":       len(ctx.get("findings", [])),
            "predictions":    len(ctx.get("predictions", [])),
            "freshness_ok":   bool(ctx.get("freshness")),
            "funnel_ok":      bool(ctx.get("funnel")),
            "schedulers_ok":  bool(ctx.get("schedulers")),
        },
        computed_at=_dt.datetime.utcnow().isoformat() + "Z",
    )
