"""Phase FF+25-followup-r9 (2026-05-20) — central brain-model config.
==========================================================================

Until now each brain layer hard-coded "claude-sonnet-4-5" in its own
file. That made it impossible to:
  · Upgrade the whole stack at once
  · Run a cost-aware tier strategy (Opus for hard problems, Sonnet for
    routine, Haiku for voice / quick reads)
  · A/B test a new model without touching 16 files

This module is the single source of truth. Each layer should now ask
brain_model_for(tier) and let env vars override.

TIERS:
  "inspector"   — Opus 4.7. Heavy synthesis: daily inspection brief,
                  novel finding generation, code-aware PR drafts.
                  Runs 1–4×/day. Highest cost per call.
  "reasoning"   — Opus by default. L7 evolving, L14 causal, L16
                  self-critique, L18 memory consolidation — anything
                  that benefits from multi-step thinking.
  "routine"     — Sonnet 4.5. L8 orchestrator, L9 conversational, L11
                  QA agent — frequent calls that need good-enough
                  judgment fast.
  "voice"       — Haiku 4.5. Brain-pulse one-liners, status summaries,
                  short labels. Cheapest, fastest.

ENV OVERRIDES (set on Railway):
  DCHUB_BRAIN_MODEL              global fallback (legacy compat)
  DCHUB_BRAIN_MODEL_INSPECTOR    override Inspector tier
  DCHUB_BRAIN_MODEL_REASONING    override reasoning tier
  DCHUB_BRAIN_MODEL_ROUTINE      override routine tier
  DCHUB_BRAIN_MODEL_VOICE        override voice tier

Note on "Mythos": as of this commit, Anthropic's public model lineup is
Opus 4.7 (1M context), Sonnet 4.5, Haiku 4.5. There is no public model
called "Mythos" — that may be a code-name from a leak or a reference to
a different vendor. If/when Anthropic ships a higher tier, drop the new
identifier into DCHUB_BRAIN_MODEL_INSPECTOR and the whole brain levels
up with one env-var change.
"""
import os
import logging
from utils.anthropic_helper import anthropic_messages_url, http_error_detail

logger = logging.getLogger(__name__)

# ── Defaults (Anthropic lineup as of 2026-05-21) ──────────────────
# Phase r33-M: upgrade Inspector + Reasoning tier to Opus 4.7 (1M
# context window). Previous attempt at "claude-opus-4-7-20251202"
# failed because we guessed the date — Anthropic accepts the
# undated alias "claude-opus-4-7" which always points at the latest
# minor revision. If a future call returns 404 (model retired),
# DCHUB_BRAIN_MODEL_INSPECTOR env var on Railway is the override.
# Falls back automatically to opus-4-5 via _safe_resolve if 4-7 errors.
# 2026-05-31 FIX: claude-opus-4-7 returns HTTP 404 from /v1/messages (not
# accessible to this account), and layer4._call_claude uses the model RAW without
# _safe_resolve — so the documented opus-4-7→opus-4-5 fallback never fired and
# every Layer-5 Claude call 404'd → 0 proposals. Point inspector/reasoning at the
# confirmed-valid claude-sonnet-4-5 (routine default; the testimonial probe uses
# it successfully). Override to a valid opus via DCHUB_BRAIN_MODEL when available.
# ── Brain v3 (2026-06-06) — Opus 4.8 1M-context upgrade ──────────
# The user upgraded their own session to Opus 4.8 (1M context) and
# wants the brain to level up to match. SAFE BY CONSTRUCTION: we set
# Opus 4.8 as the inspector + reasoning default, but every call site
# uses the full fallback chain below, so if the brain's
# ANTHROPIC_API_KEY can't reach Opus 4.8 (the prior opus-4-7 attempt
# 404'd for this account — see the 2026-05-31 note above), it
# degrades opus-4-8 → opus-4-7 → opus-4-5 → sonnet-4-5 → haiku
# automatically. Worst case = today's behavior (Sonnet); best case =
# the brain reasons on Opus 4.8.
#
# Before trusting this default, run GET /api/v1/brain/model-probe —
# it tests reachability of each tier with the brain's actual key and
# reports the best model the brain can actually use. Set the result
# into DCHUB_BRAIN_MODEL_INSPECTOR to pin it (skips the per-call
# fallback dance + saves the failed-call latency).
_DEFAULT_INSPECTOR = "claude-opus-4-8"
_DEFAULT_REASONING = "claude-opus-4-8"
_DEFAULT_ROUTINE   = "claude-sonnet-4-5"
_DEFAULT_VOICE     = "claude-haiku-4-5"
# r47 (2026-05-25): challenger tier — independent second opinion on
# proposals from the reasoning tier. A DIFFERENT model from reasoning
# (Sonnet) so the challenge is a genuine cross-model perspective, not
# the same model grading itself. L23 multi-model challenger uses this
# to gate Opus proposals before they reach human review.
_DEFAULT_CHALLENGER = "claude-sonnet-4-5"

# Brain v3: full fallback chain. opus-4-8 at the top; each rung
# degrades to the next-cheapest confirmed-or-likely-valid model.
# _safe_resolve() and call sites walk this on a 404/400 so a model
# the key can't reach never zeros out the brain.
# r-fix (2026-06-06): route AROUND the known-404 models so a fallback walk
# doesn't burn requests (+ AI Gateway error rate) hitting dead endpoints.
# claude-opus-4-7 returns 404 (see note above); claude-haiku-3-5 is RETIRED.
# On any opus failure go straight to the confirmed-valid sonnet-4-5; haiku-4-5
# is the cheapest VALID model = terminal (no fallback to retired haiku-3-5).
_FALLBACK_CHAIN = {
    # 2026-06-10: Fable 5 pinned on inspector/reasoning/challenger via env. It
    # MUST have a fallback rung or the raw call sites (e.g. layer4._call_claude,
    # which uses the model RAW) zero out the brain on any future fable-5 404.
    # Degrades fable-5 → opus-4-8 → sonnet-4-5 → haiku-4-5.
    "claude-fable-5":      "claude-opus-4-8",
    "claude-opus-4-8":     "claude-sonnet-4-5",
    "claude-opus-4-7":     "claude-sonnet-4-5",
    "claude-opus-4-5":     "claude-sonnet-4-5",
    "claude-sonnet-4-5":   "claude-haiku-4-5",
}

# Brain v3: which models support the 1M-context beta. When the
# resolved model is in this set AND DCHUB_BRAIN_1M_CONTEXT is truthy,
# call sites add the anthropic-beta: context-1m header. Lets the
# inspector tier hold the whole site's findings + history in one
# prompt instead of truncating to a 1.5KB snippet.
_ONE_M_CONTEXT_MODELS = {
    # 2026-06-10: verified fable-5 accepts the context-1m beta header (probe → 200).
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-5",
}
_ONE_M_BETA_HEADER = "context-1m-2025-08-07"

# Global fallback (matches the legacy DCHUB_BRAIN_MODEL pattern from
# brain_v2_layer4). If set, becomes the answer to every untiered call.
_GLOBAL_FALLBACK = (os.environ.get("DCHUB_BRAIN_MODEL") or "").strip()


# ── r85j: reachability-aware resolution + AUTO-ENABLE Fable ──────────
# When claude-fable-5 (currently http_404 on this account) is approved again,
# the brain auto-promotes inspector+reasoning back to it WITHOUT a manual env
# flip — and degrades to opus the moment it 404s. Mechanism: a cached
# reachability map (written by the /api/v1/brain/model-probe route, which a 2h
# cron hits) tells brain_model_for what the key can actually reach. Set
# DCHUB_BRAIN_PREFER_FABLE=1 to make fable-5 the preference for the high-reasoning
# tiers — it activates automatically the next probe after Anthropic approves it.
# SAFE BY CONSTRUCTION: fable-5 is used ONLY when a probe POSITIVELY confirms it
# reachable (it's 404 by default), so "prefer fable" runs on opus until it's back.
import time as _time
_REACH = {"map": {}, "ts": 0.0}
_REACH_TTL = 300  # re-hydrate the in-process copy from brain_meta every 5 min


def store_reachability(results: dict) -> bool:
    """Persist a {model: 'reachable'|'http_404'|...} map (from
    probe_model_reachability) so brain_model_for can degrade off dead models +
    auto-promote a model that comes back. Called by the model-probe route."""
    try:
        if not isinstance(results, dict) or not results:
            return False
        _REACH["map"] = dict(results)
        _REACH["ts"] = _time.time()
        import json as _j
        from routes.brain_v2_store import set_meta as _sm
        _sm("brain_model_reachability", _j.dumps({"ts": _REACH["ts"], "map": results}))
        return True
    except Exception:
        return False


def _load_reachability() -> dict:
    """In-process reachability map, hydrated from brain_meta (shared across
    workers + replicas) with a 5-min TTL. Empty = unknown."""
    if _REACH["map"] and (_time.time() - _REACH["ts"]) < _REACH_TTL:
        return _REACH["map"]
    try:
        import json as _j
        from routes.brain_v2_store import get_meta as _gm
        row = _gm("brain_model_reachability")
        if row and row.get("value"):
            payload = _j.loads(row["value"])
            _REACH["map"] = payload.get("map") or {}
            _REACH["ts"] = float(payload.get("ts") or _time.time())
    except Exception:
        pass
    return _REACH["map"]


def _prefer_fable_on() -> bool:
    return (os.environ.get("DCHUB_BRAIN_PREFER_FABLE") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def brain_model_for(tier: str = "routine") -> str:
    """Return the model identifier for a given tier — reachability-aware.

    Preference: DCHUB_BRAIN_PREFER_FABLE pins fable-5 on the reasoning tiers
    (auto-enable); else env override > global fallback > tier default. The
    chosen model is then validated against the cached probe: a model the key
    KNOWS it can't reach degrades to the first reachable rung of its fallback
    chain. fable-5 specifically requires POSITIVE reachability (404 by default)
    so the preference is safe even before the first probe.
    """
    tier = (tier or "routine").lower().strip()
    if _prefer_fable_on() and tier in ("inspector", "reasoning"):
        pref = "claude-fable-5"
    elif _prefer_fable_on() and tier == "challenger":
        # CROSS-MODEL CHALLENGE (restored 2026-07-01): when fable runs the
        # reasoning tiers, the challenger must be a DIFFERENT model — the
        # tier exists to give an independent second opinion, not the same
        # model grading its own homework. Env pin wins; else opus-4-8.
        pref = (os.environ.get("DCHUB_BRAIN_MODEL_CHALLENGER") or "").strip() \
            or "claude-opus-4-8"
    else:
        env_specific = (os.environ.get(f"DCHUB_BRAIN_MODEL_{tier.upper()}") or "").strip()
        pref = env_specific or _GLOBAL_FALLBACK or {
            "inspector":  _DEFAULT_INSPECTOR,
            "reasoning":  _DEFAULT_REASONING,
            "routine":    _DEFAULT_ROUTINE,
            "voice":      _DEFAULT_VOICE,
            "challenger": _DEFAULT_CHALLENGER,
        }.get(tier, _DEFAULT_ROUTINE)

    reach = _load_reachability() or {}

    def _ok(m: str) -> bool:
        st = reach.get(m, "")
        if m == "claude-fable-5":
            return st.startswith("reachable")        # require positive confirmation
        return not (st.startswith("http_") or st.startswith("error"))

    if _ok(pref):
        return pref
    for m in resolve_chain(pref):
        if m != pref and _ok(m):
            return m
    # nothing confirmed reachable — never hand back a known-404 fable; use the
    # safe reasoning default (opus) so raw call sites don't zero out.
    return _DEFAULT_REASONING if pref == "claude-fable-5" else pref


def brain_model_summary() -> dict:
    """For diagnostics: what model is each tier currently using?"""
    return {
        "inspector":  brain_model_for("inspector"),
        "reasoning":  brain_model_for("reasoning"),
        "routine":    brain_model_for("routine"),
        "voice":      brain_model_for("voice"),
        "challenger": brain_model_for("challenger"),
        "_global_fallback_env": _GLOBAL_FALLBACK or None,
        "_fallback_chain":      _FALLBACK_CHAIN,
        "_prefer_fable":        _prefer_fable_on(),       # r85j auto-enable switch
        "_reachability":        _load_reachability() or {},
    }


def fallback_for(model: str) -> str | None:
    """r33-M: if the primary model 404s (new release not yet provisioned
    on a given Anthropic key, model retired, region-restricted), call
    sites can drop down one tier and retry. Returns None if no fallback
    is known — in which case the caller should error out, not loop.

    Usage in any brain call site:

        from routes.brain_models import brain_model_for, fallback_for
        model = brain_model_for("inspector")
        for attempt in range(3):
            try:
                resp = httpx.post(API, json={"model": model, ...})
                if resp.status_code == 404 and "model" in resp.text.lower():
                    nxt = fallback_for(model)
                    if not nxt: raise
                    model = nxt
                    continue
                resp.raise_for_status()
                break
            except ...
    """
    return _FALLBACK_CHAIN.get(model)


def resolve_chain(model: str, max_depth: int = 5) -> list:
    """Brain v3: return the full degrade chain starting at `model`.

    e.g. resolve_chain("claude-opus-4-8") ->
      ["claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-5",
       "claude-sonnet-4-5", "claude-haiku-4-5"]

    Call sites walk this on a 404/400 so a model the key can't reach
    degrades all the way to a confirmed-valid model instead of giving
    up after one retry. Stops at max_depth to avoid a cycle.
    """
    chain = [model]
    cur = model
    for _ in range(max_depth):
        nxt = _FALLBACK_CHAIN.get(cur)
        if not nxt or nxt in chain:
            break
        chain.append(nxt)
        cur = nxt
    return chain


def supports_1m_context(model: str) -> bool:
    """True if `model` can take the 1M-context beta header AND the
    DCHUB_BRAIN_1M_CONTEXT env flag is on. Default OFF — 1M context
    is more expensive per call, so opt in deliberately."""
    flag = (os.environ.get("DCHUB_BRAIN_1M_CONTEXT") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return model in _ONE_M_CONTEXT_MODELS


def one_m_beta_header() -> str:
    """The anthropic-beta value to send for 1M context."""
    return _ONE_M_BETA_HEADER


def probe_model_reachability(api_key: str,
                             models: list = None,
                             timeout: int = 15) -> dict:
    """Brain v3: test which models the brain's ANTHROPIC_API_KEY can
    actually reach. Sends a 1-token probe to each model; records
    reachable / http_404 / http_401 / etc.

    This is the verify-before-claiming step: before trusting the
    opus-4-8 default, run this to see what the key can use. The
    result names the best reachable model so the operator can pin
    it into DCHUB_BRAIN_MODEL_INSPECTOR.
    """
    import json as _json
    import urllib.request
    import urllib.error

    if not api_key:
        return {"ok": False, "error": "no_api_key",
                "hint": "ANTHROPIC_API_KEY not set on the service"}

    # Probe order: best → worst. First reachable = best usable.
    # r-fix (2026-06-06): dropped claude-haiku-3-5 (RETIRED → always 404, just
    # pollutes the AI Gateway error rate). claude-opus-4-7 kept (probing it is
    # how we confirm it's still 404), but it's no longer in the fallback path.
    if models is None:
        models = ["claude-fable-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-5",
                  "claude-sonnet-4-5", "claude-haiku-4-5"]

    results = {}
    best_reachable = None
    for m in models:
        body = _json.dumps({
            "model": m,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        req = urllib.request.Request(
            anthropic_messages_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _ = r.read()
            results[m] = "reachable"
            if best_reachable is None:
                best_reachable = m
        except urllib.error.HTTPError as e:
            # 404/400 = model not available to this key.
            # 429 = rate-limited but the model EXISTS (count as reachable).
            # ★ 2026-09-01: all six models reported a bare
            # "reachable_rate_limited" while a Cloudflare AI Gateway spend rule
            # refused every one of them pre-auth — the body said so and this
            # threw it away. The status TOKEN is unchanged (brain_model_for
            # gates on startswith("reachable") / startswith("http_")); the
            # cause is appended after it.
            _d = http_error_detail(e, 160)
            if e.code == 429:
                results[m] = "reachable_rate_limited" + (f": {_d}" if _d else "")
                if best_reachable is None:
                    best_reachable = m
            else:
                results[m] = f"http_{e.code}" + (f": {_d}" if _d else "")
        except Exception as e:
            results[m] = f"error: {str(e)[:60]}"

    return {
        "ok":              True,
        "results":         results,
        "best_reachable":  best_reachable,
        "current_default": brain_model_for("inspector"),
        "recommendation": (
            f"Set DCHUB_BRAIN_MODEL_INSPECTOR={best_reachable} on Railway "
            f"to pin the brain to its best reachable model (skips the "
            f"per-call fallback dance)."
            if best_reachable else
            "No model reachable — check ANTHROPIC_API_KEY validity + "
            "account model access."
        ),
    }


def _smoke():
    s = brain_model_summary()
    logger.info(f"[brain-models] inspector={s['inspector']} "
                f"reasoning={s['reasoning']} routine={s['routine']} "
                f"voice={s['voice']}")

_smoke()
