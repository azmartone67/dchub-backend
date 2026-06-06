"""Daily-commit throttle for the brain autopilot.

Why this exists: 2026-06-05 — the autopilot landed 75 commits in 24h
(11 honest-numbers purges, 7 site-valuation iterations, 7 OG-card
changes, 2 SEO landings that 403'd on the CF edge, etc.). That churn:

  • Pushed dchub.cloud cache-hit ratio from ~39% → 23.95%
  • Created multiple Railway 503 windows during deploys
  • Shipped 7 SEO landings that 403'd at the CF edge for hours,
    causing Google to deprioritize the domain
  • Changed schema.org / meta description content mid-crawl, kicking
    SEO signals

This module gives autopilot a single function to call BEFORE any
git operation:

    from routes.brain_commit_throttle import check_daily_commit_budget
    ok, reason = check_daily_commit_budget()
    if not ok:
        return {"throttled": True, "reason": reason}
    # ... proceed with commit

The default budget is 12 commits/day per repo. The env var
`DCHUB_AUTOPILOT_DAILY_COMMIT_CAP` overrides at runtime. Manual
commits (human, not via the brain autopilot path) DO NOT count
toward this budget — only commits made by the autopilot bypass.

The throttle is keyed by date (UTC) so it resets at 00:00 UTC. A
hard kill-switch env var `DCHUB_AUTOPILOT_DISABLED` bypasses the
budget check and refuses ALL autopilot commits regardless of count.

Also exposes /api/v1/brain/commit-throttle for observability — show
the current budget consumption + remaining headroom.
"""
from __future__ import annotations

import os
import subprocess
import datetime as _dt
from flask import Blueprint, Response, jsonify

brain_commit_throttle_bp = Blueprint("brain_commit_throttle", __name__)


# ── Config ──────────────────────────────────────────────────────

_DEFAULT_CAP = 12
_AUTOPILOT_MARKER = "Co-Authored-By: Claude"  # all autopilot commits include this


def _today_utc() -> _dt.date:
    """Use UTC date for cross-timezone consistency. Resets at 00:00 UTC."""
    return _dt.datetime.utcnow().date()


def _config_cap() -> int:
    """Read the runtime cap. Default 12; settable via env."""
    try:
        v = int(os.environ.get("DCHUB_AUTOPILOT_DAILY_COMMIT_CAP", _DEFAULT_CAP))
        return max(1, min(v, 100))
    except Exception:
        return _DEFAULT_CAP


def _is_killed() -> bool:
    """Hard kill-switch: refuse all autopilot commits when set."""
    return bool(os.environ.get("DCHUB_AUTOPILOT_DISABLED"))


def _count_autopilot_commits_today() -> int:
    """Count git commits today (UTC) that carry the autopilot marker.

    Falls back to 0 on any error so the throttle never blocks the
    pipeline on a measurement failure (graceful degradation).
    """
    since = _dt.datetime.combine(_today_utc(), _dt.time(0, 0, 0)).isoformat()
    try:
        out = subprocess.check_output(
            ["git", "log", f"--since={since}Z", "--pretty=%H%n%B%n---END---"],
            timeout=10,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore")
    except Exception:
        return 0
    # Each commit body ends with our literal sentinel; count those
    # carrying the autopilot marker. (Human commits without the
    # Co-Authored-By line are NOT counted.)
    blocks = [b for b in out.split("---END---") if b.strip()]
    return sum(1 for b in blocks if _AUTOPILOT_MARKER in b)


_DEFAULT_MIN_INTERVAL_MIN = 12


def _min_interval_min() -> int:
    try:
        return max(0, int(os.environ.get(
            "DCHUB_AUTOPILOT_MIN_COMMIT_INTERVAL_MIN", _DEFAULT_MIN_INTERVAL_MIN)))
    except Exception:
        return _DEFAULT_MIN_INTERVAL_MIN


def _minutes_since_last_autopilot_commit():
    """Minutes since the most recent autopilot-marked commit, or None.

    A min-interval complements the daily cap: the cap bounds total/day but
    lets the autopilot ship in tight bursts, which keeps Railway rebuilding
    (each push = a deploy) and delays every ship. Spacing commits out smooths
    the deploy pipeline. Graceful: returns None on any git error."""
    try:
        out = subprocess.check_output(
            ["git", "log", "-40", "--pretty=%cI%x09%B%n---END---"],
            timeout=10, stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore")
    except Exception:
        return None
    for block in out.split("---END---"):
        block = block.strip()
        if not block:
            continue
        if _AUTOPILOT_MARKER in block:
            iso = block.split("\t", 1)[0].split("\n", 1)[0].strip()
            try:
                ts = _dt.datetime.fromisoformat(iso)
                now = _dt.datetime.now(ts.tzinfo)
                return max(0.0, (now - ts).total_seconds() / 60.0)
            except Exception:
                return None
    return None


# ── Public API ──────────────────────────────────────────────────

def check_daily_commit_budget(source: str = "autopilot") -> tuple[bool, str]:
    """Return (ok, reason). Call BEFORE attempting an autopilot commit.

    ok=True   → safe to proceed with commit
    ok=False  → throttled; do not commit; surface `reason` to logs/UI
    """
    if _is_killed():
        return False, ("DCHUB_AUTOPILOT_DISABLED is set — autopilot commits "
                       "globally disabled. Unset the env var to re-enable.")
    cap = _config_cap()
    n = _count_autopilot_commits_today()
    if n >= cap:
        return False, (
            f"daily commit budget exhausted: {n}/{cap} autopilot commits "
            f"shipped today (UTC). Reset at 00:00 UTC. Source attempting: "
            f"{source}. To raise the cap, set DCHUB_AUTOPILOT_DAILY_COMMIT_CAP."
        )
    gap = _min_interval_min()
    if gap > 0:
        mins = _minutes_since_last_autopilot_commit()
        if mins is not None and mins < gap:
            return False, (
                f"min-interval throttle: last autopilot commit {mins:.0f} min "
                f"ago (< {gap} min). Batch the change and retry shortly so the "
                f"deploy pipeline doesn't churn. Tune via "
                f"DCHUB_AUTOPILOT_MIN_COMMIT_INTERVAL_MIN (0 disables). Source: {source}."
            )
    return True, f"budget ok: {n}/{cap} used today, last >{_min_interval_min()}min ago (from {source})"


def get_budget_status() -> dict:
    """Snapshot the current throttle state — for observability + UI."""
    cap = _config_cap()
    n = _count_autopilot_commits_today()
    return {
        "date_utc":           _today_utc().isoformat(),
        "cap":                cap,
        "used":               n,
        "remaining":          max(0, cap - n),
        "throttled":          n >= cap,
        "killed":             _is_killed(),
        "marker":             _AUTOPILOT_MARKER,
        "note":               (
            "Autopilot commits are counted via the literal "
            f"{_AUTOPILOT_MARKER!r} sentinel in the commit body. "
            "Human commits without the sentinel are not counted."
        ),
    }


# ── /api/v1/brain/commit-throttle (read-only) ───────────────────

@brain_commit_throttle_bp.route("/api/v1/brain/commit-throttle",
                                 methods=["GET"])
def commit_throttle_status():
    """Observability endpoint: current autopilot-throttle state."""
    return jsonify(get_budget_status())
