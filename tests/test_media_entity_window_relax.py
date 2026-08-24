"""Guards for the 2026-08-23 editorial deadlock — the missing relaxation rung.

WHAT BROKE, measured live before this guard existed:

  /api/v1/brain/media/editorial-decision?slot=dcpi_mover returned

      post:  false
      reason "no novel data event cleared the newsworthiness bar this slot
              (event-driven cadence: better silent than repetitive)"

  while carrying NINE leads, every one at or above _NEWSWORTHY_MIN and two of
  them at 62. Their own `_novelty` annotations said why: eight × entity_window,
  one × publish_blocked. Not a single lead failed on score. The desk was not
  short of things to say — it was refusing everything it had.

  The quad's slots that week (/api/v1/linkedin-quad/status):
      08-16 4/4   08-17 4/4   08-18 2/2   08-19 4/4
      08-20 4/4   08-21 2/4   08-22 0/4   08-23 1/4
  and the only slot that could fire at all on the bad days was 16:00, which is
  exempt from the novelty gates via reserved_slot_bypass.

  ROOT CAUSE: editorial_decision()'s "relaxation ladder" never relaxed the gate
  that was actually binding. Rung 2 (`relaxed`) drops the KIND cooldown only;
  rung 3 (stale-rerun) asserts `not (ent and ent in entity_window)` outright.
  entity_window was therefore ABSOLUTE — in a ladder whose stated purpose since
  r86e (2026-06-17) is "fall back to the best newsworthy lead instead of
  silence". With 7 non-capability leads on the board and a 14-day window that
  ceiling is ~3.5 posts/week against 21 non-capability slots/week.

  Third recurrence of this class: 06-17 (r86e), 07-24 (reserved_slot_bypass,
  which fixed the reserved slot alone and left the general gate), 08-23 (this).

Every test that proves the rung FIRES is paired with a control proving the same
board goes dark without it — a guard that cannot tell the two apart never
protected anything. The four tests after that pin what the rung must NOT relax.

Pure: no DB, no network, never imports main.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── the live 2026-08-23 board, transcribed from /api/v1/brain/media/data-leads ─
# Scores are the measured raw_score values. Every entity is inside the 14-day
# window in the fixtures below, which is exactly the state that went dark.
_BOARD = [
    {"kind": "cap_agent_memory",  "dedup_key": "cap:agent_memory",   "score": 62.0,   "raw_score": 62.0},
    {"kind": "cap_weekly_ledger", "dedup_key": "cap:weekly_ledger",  "score": 62.0,   "raw_score": 62.0},
    {"kind": "interconnection",   "dedup_key": "queue:neso",         "score": 43.0,   "raw_score": 50.0},
    {"kind": "tenant",            "dedup_key": "tenant:top:amazon web services", "score": 27.6, "raw_score": 27.6},
    {"kind": "deal",              "dedup_key": "deal:ares:",         "score": 18.67,  "raw_score": 22.8},
    {"kind": "dcpi_build",        "dedup_key": "build:midland–odessa", "score": 18.19, "raw_score": 21.425},
    {"kind": "analyst_note",      "dedup_key": "analyst_note:2026-08-17", "score": 16.0, "raw_score": 16.0},
    {"kind": "dcpi_build",        "dedup_key": "build:tulsa",        "score": 15.55,  "raw_score": 18.312},
    {"kind": "dcpi_build",        "dedup_key": "build:oklahoma city", "score": 14.0,  "raw_score": 17.204},
]

# The entity tails _entity_tail() derives from the keys above. Hardcoded, not
# derived, so a change to _entity_tail() breaks this list loudly instead of
# silently re-aiming every ledger fixture below at whatever it now returns.
# ★ 'analyst_note:2026-08-17' -> '20260817', NOT 'analystnote20260817': the
# tail is everything after the FIRST colon, so the kind is not part of it.
_TAILS = ["agentmemory", "weeklyledger", "neso", "topamazonwebservices",
          "ares", "midlandodessa", "20260817", "tulsa",
          "oklahomacity"]


def test_the_fixture_tails_match_what_entity_tail_actually_returns():
    """★ THE FIXTURE'S OWN GUARD. Every test below puts _TAILS into the ledger
    to place the board inside the entity window. A tail that does not match
    leaves its lead FRESH, the strict filter answers first, and the test passes
    while exercising nothing — which is exactly how the first draft of this
    file reported green on a rung that never ran."""
    from routes.media_editorial import _entity_tail
    assert [_entity_tail(l) for l in _BOARD] == _TAILS


@pytest.fixture()
def med(monkeypatch):
    """media_editorial with every external read stubbed."""
    import routes.media_editorial as m
    monkeypatch.delenv("MEDIA_ENTITY_WINDOW_RELAX_DISABLE", raising=False)
    monkeypatch.delenv("MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE", raising=False)
    monkeypatch.delenv("MEDIA_EDITORIAL_REST_DAYS", raising=False)
    monkeypatch.setattr(m, "_conn", lambda: None)
    monkeypatch.setattr(m, "_recently_posted_keys", lambda **k: set())
    monkeypatch.setattr(m, "_topic_mix_weights", lambda: {})
    monkeypatch.setattr(m, "_semantic_repeat_predicate",
                        lambda ranked: (lambda lead: False))
    monkeypatch.setattr(m, "recent_lead_ledger", lambda **k: [])
    monkeypatch.setattr(m, "recent_publish_blocked_keys", lambda **k: set())
    monkeypatch.setattr(m, "rank_data_events", lambda: [dict(x) for x in _BOARD])
    return m


def _ledger(med, monkeypatch, days_ago, tails=None):
    """Put every board entity inside the entity window, `days_ago` old."""
    monkeypatch.setattr(med, "recent_lead_ledger", lambda **k: [
        {"kind": "other", "entity": t, "days_ago": days_ago}
        for t in (tails if tails is not None else _TAILS)])


# ── 1. the outage itself ────────────────────────────────────────────────────
def test_the_live_0823_board_does_not_go_dark(med, monkeypatch):
    """★ THE OUTAGE, DIRECTLY. Nine leads, all above the bar, all rested well
    past _rest_days but all still inside the 14-day entity window. Before the
    rung this returned post:false with a full board underneath it."""
    _ledger(med, monkeypatch, days_ago=9.0)
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is True, "a full board of rested leads must not go dark"
    assert out["entity_window_relaxed"] is True
    assert out["lead"]["dedup_key"] == "cap:agent_memory"   # best by score


def test_control_the_same_board_goes_dark_with_the_rung_disabled(med, monkeypatch):
    """★ NEGATIVE CONTROL — the PRE-FIX behaviour, reachable via the kill
    switch. If this ever stops failing to post, the test above has stopped
    proving anything and the rung is not what is producing the post."""
    monkeypatch.setenv("MEDIA_ENTITY_WINDOW_RELAX_DISABLE", "1")
    _ledger(med, monkeypatch, days_ago=9.0)
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is False
    assert out["entity_window_relaxed"] is False
    assert all(l["_novelty"].startswith("entity_window:") for l in out["ranked"])


def test_the_verdict_says_the_window_was_relaxed(med, monkeypatch):
    """The operator asked to SEE when this fires. post:true must not hide that
    it took the last rung — a silent relaxation is how the next audit gets
    told the board was fresh when it was not."""
    _ledger(med, monkeypatch, days_ago=9.0)
    out = med.editorial_decision(slot="dcpi_mover")
    assert "entity-window relaxed" in out["reason"]
    assert "9-lead board" in out["reason"]


# ── 2. what the rung must NOT relax ─────────────────────────────────────────
def test_the_rest_period_is_real_not_abolished(med, monkeypatch):
    """★ THE WHOLE POINT OF A RUNG RATHER THAN A DELETION. Every entity led
    ONE day ago — inside _rest_days — so relaxing 14d must still leave them
    ineligible. If this fails the change is not a relaxation, it is the
    removal of the repetition guard the desk exists to enforce."""
    _ledger(med, monkeypatch, days_ago=1.0)
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is False, "a lead that led yesterday must not rerun"
    assert out["entity_window_relaxed"] is False


def test_rest_boundary_is_honored_on_both_sides(med, monkeypatch):
    """The boundary itself, pinned: 4.9d rests short, 5.1d rests long. A guard
    that only tests the far side cannot catch an inverted comparison."""
    _ledger(med, monkeypatch, days_ago=4.9)
    assert med.editorial_decision(slot="dcpi_mover")["post"] is False
    _ledger(med, monkeypatch, days_ago=5.1)
    assert med.editorial_decision(slot="dcpi_mover")["post"] is True


def test_the_rung_never_relaxes_publish_blocked(med, monkeypatch):
    """★ A lead the publisher keeps REFUSING is not a novelty question. The
    2026-08-15 open loop was the desk re-electing a lead the gate had already
    refused eight times; relaxing the entity window must not re-open it."""
    _ledger(med, monkeypatch, days_ago=9.0)
    monkeypatch.setattr(med, "recent_publish_blocked_keys",
                        lambda **k: set(_TAILS))
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is False
    assert out["entity_window_relaxed"] is False


def test_the_rung_never_relaxes_semantic_repeat(med, monkeypatch):
    """A reworded near-repeat of a recent post is a BAD post, not a stale one.
    The theme guard is about quality and is never a cadence lever."""
    _ledger(med, monkeypatch, days_ago=9.0)
    monkeypatch.setattr(med, "_semantic_repeat_predicate",
                        lambda ranked: (lambda lead: True))
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is False
    assert out["entity_window_relaxed"] is False


def test_the_rung_never_relaxes_the_newsworthiness_bar(med, monkeypatch):
    """Cadence is not a reason to publish something that was never
    newsworthy. Below _NEWSWORTHY_MIN stays below it."""
    monkeypatch.setattr(med, "rank_data_events", lambda: [
        {"kind": "dcpi_build", "dedup_key": "build:tulsa",
         "score": 2.0, "raw_score": 2.0}])
    _ledger(med, monkeypatch, days_ago=9.0, tails=["tulsa"])
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is False
    # ★ `post is False` ALONE does not prove this — the final newsworthiness
    # gate rejects a below-bar lead whether or not the rung also checked. The
    # observable difference is the LABEL: a rung that elects an unpublishable
    # lead reports having relaxed the window for a post that never happened.
    # (Mutation-verified: dropping the bar from the rung survives without this.)
    assert out["entity_window_relaxed"] is False


# ── 3. the rung is LAST ─────────────────────────────────────────────────────
def test_a_fresh_lead_still_wins_over_a_relaxed_one(med, monkeypatch):
    """★ ORDERING. The rung must run only when silence is the alternative. If
    it preempts the strict filter, the entity window stops meaning anything
    and the desk repeats itself while fresh leads sit unused."""
    # Only `neso` is inside the window; every other entity is untouched, so the
    # strict filter has candidates and the rung must not be reached.
    _ledger(med, monkeypatch, days_ago=9.0, tails=["neso"])
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is True
    assert out["entity_window_relaxed"] is False, \
        "the rung fired while a fresh lead was available"
    # ★ The ladder can re-elect the SAME lead it would have picked fresh, so
    # comparing only the lead cannot see it run. The flags are the witness.
    # (Mutation-verified: relaxing the ladder's `top is None` preamble survives
    # a lead-identity assertion and is caught only by this one.)
    assert out["stale_fallback"] is False, \
        "the stale ladder ran while a fresh lead was available"
    assert out["lead"]["dedup_key"] == "cap:agent_memory"


# ★ NO TEST FOR THE absent-from-ledger SENTINEL, deliberately. The first draft
# had one; it passed against a mutant that changed the default from `inf` to
# `0.0`, i.e. it proved nothing, so it was removed rather than left to look
# like coverage. The reason it cannot be written:
#
#   Rung 4 differs from rung 3 by EXACTLY ONE term — the entity_window check.
#   So rung 4 is reached only for a candidate rung 3 rejected, and for any
#   candidate whose entity is NOT in entity_window rung 3 (or rung 2) already
#   accepts it. Rung 4 therefore only ever evaluates entities that ARE in
#   entity_window — and entity_window and entity_last_led are built from the
#   same ledger rows, so such an entity is always present in the map.
#
# The `float("inf")` default is dead code kept as a fail-safe: if a later
# change decouples those two structures, defaulting a never-seen entity to
# "0 days ago" would silently make never-posted leads the only ones the rung
# refuses. Marked here so nobody "adds the missing test" and gets a green one.
