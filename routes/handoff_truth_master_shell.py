"""handoff_truth_master_shell.py — master shell #44: HANDOFF TRUTH (2026-07-30).

The 7-day dashboard read that triggered this shell showed the agent→human
funnel dying at a wall no human could pass: 374 paywall hits → 265 high-intent
→ 264 relay links minted → **0 humans acted** — and the zero was STRUCTURAL.
The claim token is single-use and the gateway auto-redeems it in median 0.85s
("binds the trial key with NO human page-open" is the redeem contract), so
every human click since launch landed on 410 Gone, and the human-open
instrument (claim_page_opened_at) fired zero times all-time.

That zero contaminated a strategic decision: "the human buyer does not exist"
was concluded from an instrument that could not observe humans. This shell is
the repair — of the instrument first, and of the epistemics around it.

WHAT SHIPPED WITH THIS SHELL (the core build, in the claim module):
  · a SECOND artifact at relay mint: /relay/<token> — human-audience only
    (KIND_HUMAN_VIEW), 7-day TTL, multi-open, binds NOTHING on open. The
    agent's single-use auto-redeemed token is UNCHANGED — machine-mediated
    handoff is by design and working (264 successful agent binds/week).
  · audience separation enforced at every door: /claim and /redeem bounce or
    403 human tokens; /relay bounces agent tokens.
  · the funnel's human_acted stage made measurable for the first time, with
    v1 kept as a labelled legacy diagnostic and the discontinuity declared
    where consumers read. [The stage has been redefined twice since this shell
    shipped. This sentence used to pin the number of the day — it said v2,
    then carried a bracketed "the funnel is at DEFINITION v3" patch, and was
    still saying v3 when the API published v4 on 2026-08-17. It no longer
    names a version: the canon is routes/handoff_definition, published at
    definitions.human_acted, and lane A's state block renders it. Lane A's own
    counts still read the RAW /relay stamps, which include probe opens — the
    funnel's number is the probe- and operator-excluded canonical.]

WHAT THIS CONDUCTOR DOES: four read-only lanes, no actuators. Outward sends
stay human-gated by standing policy; nothing here contacts anyone.

  A  handoff_instrument — the funnel around the new artifact: mints, human
     opens (the new instrument), redeems, identity, paid. UNMEASURED-honest:
     zero opens in week one may mean agents don't SURFACE the link, not that
     humans decline. Judge after weeks.
  B  pending_human_sends — the outbound artifacts awaiting a HUMAN send
     decision as of this shell's ship date (partner replies, X drafts).
     Static by design: the shell must never send, so it can only remind.
  C  in_flight_watch — live probes of the things deliberately left to
     accumulate: the per-platform attribution gate (opens 2026-08-04 at the
     earliest), and the two representation-drift fixes (/agent tool count,
     /ai hero facility count).
  D  hygiene_targets — named, measured, NOT built here: the /ai platform
     cards reading a legacy counter (HuggingFace card said "1 total" while
     the live feed showed HF hitting /api/ai/cite in real time), and the
     pending-platform outreach inventory.

Endpoint (admin-keyed, read-only):
  GET /api/v1/admin/handoff-truth/state
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, request

# Canon tool count for the /agent probe — PINNED, not a literal, so the probe
# moves with ai_surface_canon and cannot itself re-freeze at a retired count
# (the exact drift it watches for). Leaf import, agent_concierge precedent.
from ai_surface_canon import PINNED as _CANON

# Same reasoning one metric over: the human_acted definition is PUBLISHED, so
# this shell renders it rather than describing it. Leaf import (pure data +
# SQL-string assembly, no Flask, no DB).
from routes.handoff_definition import (
    human_acted_definition as _human_acted_definition,
    human_acted_sentence as _human_acted_sentence,
)

logger = logging.getLogger("handoff_truth")
handoff_truth_bp = Blueprint("handoff_truth", __name__)

SHELL_NUMBER = 44
SHELL_NAME = "handoff-truth"

# The date the two-artifact split shipped — lane A's verdicts key off how
# long the instrument has actually been live, so a day-two zero reads
# ACCUMULATING, never as evidence about demand.
INSTRUMENT_LIVE_DATE = date(2026, 7, 30)
MIN_WEEKS_BEFORE_DEMAND_VERDICT = 2

# Lane B: outbound artifacts awaiting a human send decision AS OF SHIP DATE.
# Deliberately static — this shell must never send, so it can only remind.
# Mark items sent by editing this list in a PR; that keeps the send trail in
# git history, which is the point.
PENDING_HUMAN_SENDS = (
    {"artifact": "partner reply drafts r1-r4 (7 platforms)",
     "where": "~/Downloads/dchub-ai-partner-responses-agent-success*-2026-07-30.md",
     "queued": "2026-07-30",
     # Confirmed by the responses themselves: all seven platforms replied to
     # the drafts' content across rounds 1-4 on the same day.
     "sent": "2026-07-30"},
    {"artifact": "Grok X post drafts (2 variants, re-pull numbers before post)",
     "where": "in r1/r3 reply docs + Grok's own refinements",
     "queued": "2026-07-30"},
    {"artifact": "Gemini Shift A/B closing report (incl. shell-#44 P.S.)",
     "where": "~/Downloads/dchub-gemini-shift-ab-report-2026-07-30.md",
     "queued": "2026-07-30",
     # Confirmed: Gemini's reply restated the P.S. content (observer-effect
     # framing + the dispatch-table diagram) and signed off for Aug 4.
     "sent": "2026-07-30"},
)


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _probe(url: str, timeout: int = 10):
    """Tiny GET with a self-identifying UA (classifies internal, never counts
    as an agent). requests, not urllib — the regression lint bans
    urllib.request on Railway (#1940 class). Returns (status, body_prefix)
    or (None, error)."""
    try:
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "dchub-shell44-handoff-truth/1.0",
                                  "Cache-Control": "no-cache"})
        return r.status_code, (r.text or "")[:65536]
    except Exception as e:
        return None, str(e)[:120]


def _lane_a_instrument(days: int = 7) -> dict:
    """The funnel around the new artifact — own queries, same tables the
    public funnel reads, plus the open-COUNT the funnel's distinct-session
    view can't show."""
    lane = {
        "lane": "A/handoff_instrument",
        "instrument_live_since": INSTRUMENT_LIVE_DATE.isoformat(),
        "window_days": days,
        "status": "UNMEASURED",
        # DERIVED, never restated (r-definition-one-writer, 2026-08-18). This
        # shell's docstring and this lane's verdict both used to type the
        # version into prose, and both were describing v3 the day the API
        # published v4. The block is the one the funnel publishes.
        "human_acted_definition": _human_acted_definition(),
        "human_acted_definition_note": _human_acted_sentence(),
    }
    c = _conn()
    if c is None:
        lane["status"] = "UNAVAILABLE"
        lane["error"] = "no database connection"
        return lane
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT
                     COUNT(*) FILTER (WHERE claim_minted_at IS NOT NULL
                                        AND claim_minted_at > NOW() - make_interval(days => %s)) AS minted,
                     COUNT(*) FILTER (WHERE human_view_first_opened_at IS NOT NULL
                                        AND human_view_first_opened_at > NOW() - make_interval(days => %s)) AS human_opened,
                     COALESCE(SUM(human_view_opens) FILTER (
                          WHERE human_view_first_opened_at > NOW() - make_interval(days => %s)), 0) AS human_opens_total,
                     COUNT(*) FILTER (WHERE claim_used_at IS NOT NULL
                                        AND claim_used_at > NOW() - make_interval(days => %s)) AS redeemed,
                     COUNT(*) FILTER (WHERE claim_email IS NOT NULL AND claim_email <> ''
                                        AND last_hit_at > NOW() - make_interval(days => %s)) AS identified
                   FROM mcp_high_intent_sessions""",
                (days, days, days, days, days),
            )
            minted, opened, opens_total, redeemed, identified = \
                [int(x or 0) for x in cur.fetchone()]
            lane.update(relays_minted=minted, human_first_opens=opened,
                        human_opens_total=opens_total, redeemed=redeemed,
                        identified=identified)
            days_live = (datetime.now(timezone.utc).date()
                         - INSTRUMENT_LIVE_DATE).days
            lane["days_instrument_live"] = days_live
            if days_live < 7 * MIN_WEEKS_BEFORE_DEMAND_VERDICT:
                lane["status"] = "ACCUMULATING"
                lane["verdict"] = (
                    "instrument live; demand verdict deliberately withheld "
                    f"until ≥{MIN_WEEKS_BEFORE_DEMAND_VERDICT} weeks of data — "
                    "a zero this early may mean agents don't surface the link "
                    "(the mint payload's human_note asks them to), not that "
                    "humans decline")
            elif minted == 0:
                lane["status"] = "UNMEASURED"
                lane["verdict"] = "no relays minted in the window — nothing to judge"
            else:
                lane["status"] = "MEASURED"
                lane["verdict"] = (
                    "instrument mature — the human-demand question the 'buyer "
                    "does not exist' decision needed is now answerable from "
                    "human_first_opens/relays_minted; this lane's "
                    "human_first_opens is the RAW stamp count INCL. probes, "
                    "so judge it against the funnel's canonical stage, whose "
                    "definition is rendered in this lane's "
                    "human_acted_definition block (never restated here — the "
                    "restated version in this sentence was two releases "
                    "stale), and re-open the funnel decision only on THIS "
                    "data")
    except Exception as e:
        lane["status"] = "UNAVAILABLE"
        lane["error"] = str(e)[:150]
    finally:
        try: c.close()
        except Exception: pass
    return lane


def _lane_b_pending_sends() -> dict:
    return {
        "lane": "B/pending_human_sends",
        "status": "PENDING_HUMAN",
        "policy": "outward sends are human-gated by standing rule; this shell "
                  "reminds and never sends. Mark items sent by editing "
                  "PENDING_HUMAN_SENDS in a PR — the send trail belongs in git.",
        "items": list(PENDING_HUMAN_SENDS),
    }


def _lane_c_in_flight() -> dict:
    lane = {"lane": "C/in_flight_watch", "probes": {}}
    st, body = _probe("https://dchub.cloud/api/v1/reports/agent-success")
    gate = {}
    if st == 200:
        try:
            payload = json.loads(body)
            reach = payload["sections"][0]
            g = reach["per_platform"]["gate"]
            gate = {"status": reach["per_platform"]["status"],
                    "passed": g["passed"],
                    "generic_bucket_share_7d": g["generic_bucket_share_7d"],
                    "days_since_fix": g["days_since_fix"],
                    "earliest_eligible": g["earliest_eligible"]}
        except Exception as e:
            gate = {"error": f"payload shape: {str(e)[:80]}"}
    else:
        gate = {"error": f"http {st}: {str(body)[:80]}"}
    lane["probes"]["attribution_gate"] = gate

    st, body = _probe("https://dchub.cloud/agent")
    _want_tools = "%d tools" % _CANON["tools_advertised"]
    lane["probes"]["agent_page_tool_count"] = {
        "ok": bool(st == 200 and _want_tools in (body or "")),
        "note": "expects the canon count '%s' in the served page (healed "
                "2026-07-30; canon-derived 2026-07-31)" % _want_tools,
    } if st else {"ok": False, "error": str(body)[:80]}

    st, body = _probe("https://dchub.cloud/ai")
    if st == 200:
        stale = "12,650+" in (body or "")
        lane["probes"]["ai_hero_facility_count"] = {
            "ok": not stale,
            "note": ("stale '12,650+' literal still served — the sweep session "
                     "has not landed or CF cache holds the old copy"
                     if stale else "hero no longer serves the stale literal"),
        }
    else:
        lane["probes"]["ai_hero_facility_count"] = {"ok": False,
                                                    "error": str(body)[:80]}
    lane["status"] = "WATCHING"
    return lane


def _lane_d_hygiene() -> dict:
    return {
        "lane": "D/hygiene_targets",
        "status": "NAMED_NOT_BUILT",
        "items": [
            {"target": "/ai platform cards read a legacy cumulative counter",
             "evidence": "2026-07-30: HuggingFace card said '1 total' while the "
                         "live feed showed HF hitting /api/ai/cite in real time; "
                         "You.com similar. Same class as counts→mcp_calls_identity "
                         "canonical wiring.",
             "fix": "repoint the cards to the canonical identity counts — its "
                    "own PR, not this shell's"},
            {"target": "pending-platform outreach (Kimi, Qwen, Poe, MiniMax, "
                       "Windsurf, Z.ai await first request)",
             "evidence": "dashboard 'Pending' cards 2026-07-30",
             "fix": "registry listings exist; outreach sends are human-gated"},
        ],
    }


@handoff_truth_bp.route("/api/v1/admin/handoff-truth/state", methods=["GET"])
def handoff_truth_state():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except Exception:
        days = 7
    return jsonify({
        "ok": True,
        "shell": SHELL_NUMBER,
        "name": SHELL_NAME,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # The version half of this sentence is BUILT from the published
        # definition. Typed, it said v3 while the API served v4.
        "core_build": "two-artifact relay handoff (agent token unchanged; "
                      "human /relay view multi-open+non-binding); "
                      + _human_acted_sentence(),
        "lanes": [
            _lane_a_instrument(days),
            _lane_b_pending_sends(),
            _lane_c_in_flight(),
            _lane_d_hygiene(),
        ],
        "actuators": "NONE — read-only by construction; outward sends stay "
                     "human-gated (standing policy)",
    })
