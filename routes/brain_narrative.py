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

logger = logging.getLogger(__name__)
brain_narrative_bp = Blueprint("brain_narrative", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

_NARRATIVE_CACHE = {"text": None, "computed_at": 0.0, "based_on_count": 0}
_NARRATIVE_TTL = 300  # 5 min


def _fetch_findings() -> list[dict]:
    """Pull current brain findings."""
    try:
        import requests
        r = requests.get("http://localhost:8080/api/v1/brain/consistency-radar",
                         timeout=10)
        return (r.json() or {}).get("findings") or []
    except Exception:
        return []


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
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _ANTHROPIC_KEY,
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
    """Force a Claude-narrative recompute. Costs ~$0.001/call (haiku)."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if request.method == "POST" and _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    if not _ANTHROPIC_KEY:
        return jsonify(ok=False,
                       error="ANTHROPIC_API_KEY not set"), 503

    findings = _fetch_findings()
    commits = _recent_commits(days=1)
    if not findings:
        return jsonify(ok=False, error="No findings to narrate"), 503

    prompt = _build_narrative_prompt(findings, commits)
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
