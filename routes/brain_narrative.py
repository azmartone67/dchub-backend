"""
Phase ZZZZ-brain-narrative (2026-05-18) — Brain L2: reason about findings.

Closes the gap "brain has 50 findings but no story." This endpoint pulls
current findings + last-24h git log + last-24h cron failures, asks Claude
to synthesize a 3-sentence "what's going on" narrative, and caches it.

Becomes the new top-line of the brain dashboard: instead of staring at a
flat list of 43 findings, the operator reads "this week 4 of 5 stale-
surface issues trace back to the iso_metrics cron not firing — fix that
one cron and 8 findings clear." Brain gets a voice.

  GET /api/v1/brain/narrative   — cached narrative (5min TTL)
  POST /api/v1/brain/narrative/refresh   — force recompute (admin)
"""

import os
import json
import time
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify
from utils.anthropic_helper import anthropic_messages_url
from routes.brain_llm_spend import instrumented_post as _llm_post
from utc_clock import utc_now

logger = logging.getLogger(__name__)
brain_narrative_bp = Blueprint("brain_narrative", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

_NARRATIVE_CACHE = {"text": None, "computed_at": 0.0, "based_on_count": 0}
_NARRATIVE_TTL = 300  # 5 min


# ── COST SAFETY: flag (ships dark) + server-side daily cap ───────────
# This endpoint is hit by a cron ~4x/day with ONLY an admin + key check, so
# with ANTHROPIC_API_KEY set it fires guaranteed model calls with no opt-out.
# Mirror routes/brain_self_director.py: ship DARK behind a default-OFF flag and
# a server-side daily cap, both enforced cost-first BEFORE any model call.
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """The narrative refresh ships DARK. Default OFF so a cron-fired loop can't
    burn API budget until an operator flips the flag. When off, refresh is a
    NO-OP that makes ZERO model calls."""
    return _truthy(os.environ.get("BRAIN_NARRATIVE_ENABLED"))


def _daily_cap() -> int:
    """Server-side daily cap on narrative recomputes — the cost ceiling.
    Default 4 (the cron fires ~4x/day). Enforced in the handler, NOT trusted to
    the cron schedule."""
    try:
        return max(0, int(os.environ.get("BRAIN_NARRATIVE_DAILY_CAP", "4")))
    except Exception:
        return 4


# In-memory daily counter, keyed by UTC date. This endpoint has no natural
# output table (it caches in _NARRATIVE_CACHE only), so a best-effort module-
# level counter is the cap backstop — the FLAG is the primary control. Fails
# CLOSED on any error (returns a huge number) so a broken counter never lets the
# loop run uncapped.
_DAILY_COUNTER = {"date": None, "count": 0}


def _utc_today() -> str:
    return utc_now().strftime("%Y-%m-%d")


def _today_count() -> int:
    """How many narrative recomputes ran TODAY (UTC). Returns a LARGE number on
    any error so a broken counter fails CLOSED (skips the model call) rather
    than letting the loop run uncapped."""
    try:
        today = _utc_today()
        if _DAILY_COUNTER.get("date") != today:
            return 0
        return int(_DAILY_COUNTER.get("count") or 0)
    except Exception:
        return 10 ** 9


def _bump_today_count() -> None:
    """Record that a model call ran today. Best-effort; never raises."""
    try:
        today = _utc_today()
        if _DAILY_COUNTER.get("date") != today:
            _DAILY_COUNTER["date"] = today
            _DAILY_COUNTER["count"] = 0
        _DAILY_COUNTER["count"] = int(_DAILY_COUNTER.get("count") or 0) + 1
    except Exception:
        pass


def _fetch_findings() -> list[dict]:
    """Pull current brain findings. Bumped to 30s timeout because the
    brain radar's cold-start scan runs 50+ detectors in parallel and
    can take 20s+ on the first call. Subsequent calls hit the 5min
    cache and return in <100ms."""
    # ★2026-08-12 — every failure here used to return [], indistinguishable
    # from "the radar found nothing wrong". A narrative generated from a dead
    # radar therefore read as an all-clear. None means UNMEASURED; callers must
    # not render a clean bill of health from it.
    from util.internal_fetch import probe
    env = probe("/api/v1/brain/consistency-radar", 30)
    if not env["ok"]:
        return None
    return (env["data"].get("findings") or [])


def _recent_commits(days: int = 1) -> list[str]:
    """Read last N days of commit subjects via GitHub API (since git binary
    isn't on Railway)."""
    try:
        import requests
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
        since = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).isoformat() + "Z"
        h = {"Accept": "application/vnd.github+json"}
        if token: h["Authorization"] = f"Bearer {token}"
        r = requests.get(f"https://api.github.com/repos/{repo}/commits",
                         params={"since": since, "per_page": 30},
                         headers=h, timeout=10)
        if r.status_code != 200: return []
        return [(c.get("commit", {}).get("message") or "").split("\n")[0]
                for c in (r.json() or [])][:20]
    except Exception:
        return []


def _build_narrative_prompt(findings: list[dict], commits: list[str]) -> str:
    findings_summary = []
    for f in findings[:25]:
        findings_summary.append(
            f"- [{f.get('issue','?')}] {f.get('url','')}: {(f.get('detail','') or '')[:160]}"
        )
    commits_block = "\n".join(f"  • {c}" for c in commits) if commits else "  (no commits in last 24h)"
    return f"""You are the senior on-call engineer for DC Hub (dchub.cloud — a data center
intelligence platform). The "brain" detectors just produced {len(findings)}
findings. Recent git activity is at the bottom.

Your job: write a 3-paragraph operational digest for the founder.

Paragraph 1 — THE STORY: what's the dominant narrative across these findings?
(group root causes; mention only the top 2-3 themes, not every finding)

Paragraph 2 — WHAT TO FIX FIRST: pick the single highest-leverage action
(one fix that closes the most findings, OR the one most user-visible). Be
specific — name the file or endpoint.

Paragraph 3 — WHAT'S WORKING: brief callout to a positive signal (a metric
that's up, a fix that landed, an absence of findings in a previously-noisy
area). Keep this honest — if nothing's clearly working, say so.

Keep total under 200 words. No headers, no bullet lists. Direct prose.
Founder reads this in 30 seconds.

=== FINDINGS ({len(findings)}) ===
{chr(10).join(findings_summary)}

=== RECENT COMMITS (last 24h) ===
{commits_block}
"""


def _call_claude(prompt: str) -> str | None:
    """Single Claude call. Returns narrative text or None on failure."""
    if not _ANTHROPIC_KEY:
        return None
    try:
        import requests
        r = _llm_post("brain_narrative",
            anthropic_messages_url(),
            headers={
                "x-api-key": _ANTHROPIC_KEY,
                "User-Agent": "dchub-brain/1.0",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning(f"Claude API {r.status_code}: {r.text[:200]}")
            return None
        j = r.json() or {}
        blocks = j.get("content") or []
        text_parts = [b.get("text") for b in blocks if b.get("type") == "text"]
        return "".join(text_parts).strip() or None
    except Exception as e:
        logger.warning(f"Claude call failed: {e}")
        return None


@brain_narrative_bp.route("/api/v1/brain/narrative", methods=["GET"])
def narrative():
    """Cached LLM narrative of current brain state."""
    now = time.monotonic()
    if (_NARRATIVE_CACHE["text"]
            and (now - _NARRATIVE_CACHE["computed_at"]) < _NARRATIVE_TTL):
        return jsonify(
            ok=True,
            narrative=_NARRATIVE_CACHE["text"],
            based_on_findings=_NARRATIVE_CACHE["based_on_count"],
            cache_age_seconds=int(now - _NARRATIVE_CACHE["computed_at"]),
            cached=True,
        ), 200
    return jsonify(
        ok=False,
        narrative=None,
        note=("Narrative not yet computed. Call POST /api/v1/brain/narrative/refresh "
              "(admin) or wait for next 6h cron."),
    ), 200


@brain_narrative_bp.route("/api/v1/brain/narrative/refresh",
                            methods=["POST", "GET"])
def refresh_narrative():
    """Force a Claude-narrative recompute. Costs ~$0.001/call (haiku).

    COST-FIRST GUARD ORDER (every guard short-circuits WITHOUT a model call):
      1. admin check (unchanged) — non-admin POST -> 401
      2. flag off  -> {ok:false, skipped:disabled}  (ships dark; ZERO model calls)
      3. no api key-> {ok:false} 503                 (degrade gracefully)
      4. daily cap -> {ok:false, skipped:daily_cap}  (the cost ceiling)
      5. L20 durability guard (memory/concurrency/rate) -> 429
      6. else      -> the existing single Claude call (behavior UNCHANGED).
    """
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if request.method == "POST" and _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    # ── Guard: flag off → NO model call (ships dark) ─────────────────
    if not _enabled():
        return jsonify(
            ok=False, skipped_reason="disabled",
            note=("Narrative refresh is dark (default OFF). Set "
                  "BRAIN_NARRATIVE_ENABLED=1 to enable."),
        ), 200

    # ── Guard: no API key → NO model call (degrade gracefully) ───────
    if not _ANTHROPIC_KEY:
        return jsonify(ok=False,
                       error="ANTHROPIC_API_KEY not set"), 503

    # ── Guard: daily cap → NO model call (the cost ceiling) ──────────
    cap = _daily_cap()
    try:
        used = _today_count()
    except Exception:
        # Fail CLOSED on a broken counter — don't run uncapped.
        used = 10 ** 9
    if used >= cap:
        return jsonify(ok=False, skipped_reason="daily_cap",
                       used_today=used, daily_cap=cap,
                       note="Daily narrative-recompute cap reached."), 200

    # ── L20 durability guard (memory/concurrency/rate) before the call.
    # Mirrors how brain_layer8 gates; fails SAFE (permissive) if L20 import
    # errors so the narrative never hard-depends on the durability module.
    try:
        from routes.brain_layer20_durability import can_start_claude_call
        allowed, reason = can_start_claude_call("L2-narrative")
        if not allowed:
            return jsonify(ok=False, throttled=True, reason=reason), 429
    except Exception:
        pass

    findings = _fetch_findings()
    commits = _recent_commits(days=1)
    # ★2026-08-12 — this branch used to fire for BOTH "radar returned nothing"
    # and "radar could not be reached", and published an all-clear either way.
    # The old detail line admitted it could not tell them apart: "All detectors
    # clean (or radar cold-start in progress)". A narrative that says everything
    # is fine because its instrument is dead is the most expensive shape of this
    # bug in the codebase — it is written to a public surface and read by humans.
    if findings is None:
        findings = [{
            "issue": "radar_unmeasurable",
            "url": "brain/consistency-radar",
            "detail": "The consistency radar could NOT be measured on this run. "
                      "This narrative says nothing about detector state — it is "
                      "explicitly NOT an all-clear.",
        }]
    elif not findings:
        # Genuinely measured, genuinely empty. Now this really is good news.
        findings = [{
            "issue": "no_findings_currently_open",
            "url": "brain/consistency-radar",
            "detail": "Radar answered and all detectors are clean.",
        }]

    prompt = _build_narrative_prompt(findings, commits)
    # Count this recompute against the daily cap BEFORE the call so a slow/
    # failing call still consumes its slot (the cap bounds attempts, not just
    # successes — a retrying cron must not bypass the ceiling).
    _bump_today_count()
    text = _call_claude(prompt)
    if not text:
        return jsonify(ok=False, error="Claude call failed"), 503

    _NARRATIVE_CACHE["text"] = text
    _NARRATIVE_CACHE["computed_at"] = time.monotonic()
    _NARRATIVE_CACHE["based_on_count"] = len(findings)

    return jsonify(
        ok=True,
        narrative=text,
        based_on_findings=len(findings),
        based_on_commits=len(commits),
        computed_at=_dt.datetime.utcnow().isoformat() + "Z",
    ), 200
