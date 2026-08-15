"""Guards for the 2026-08-15 media-publisher outage.

WHAT BROKE, measured live before these guards existed:

  * Last successful LinkedIn post 2026-08-12T18:40Z. The next 8 slots
    (through 2026-08-15T19:04Z) ALL elected lead_kind=agent_demand /
    lead_entity=wk202633 and ALL died on
    `gate: duplicate opening hook (…) already posted within 5d`.
    recent_lead_ledger() reads `success = TRUE`, so the desk never saw the
    refusals, scored the lead `fresh` every slot, and re-elected it. The
    dedup key is week-bucketed, so nothing would change until the ISO week
    rolled. Selection and publication were each correct; the loop between
    them was open.

  * image_attached was FALSE on all 30 rows of /api/v1/linkedin-quad/status
    — including the SUCCESSFUL posts. The image upload read the raw
    LINKEDIN_ACCESS_TOKEN env var (LinkedIn: 401 REVOKED_ACCESS_TOKEN) while
    publishing used the DB token (valid another 50 days).

  * Through all of it /api/v1/media/pulse reported
    {"linkedin":{"verdict":"healthy"},"ok":true} — it counted
    auto_press_releases (the PRESS cross-post path, not the publisher that
    died) and called any count above zero healthy.

Each test below pins one of those. Several assert the NEGATIVE control too —
that the same input WITHOUT the fix produces the broken answer — because a
guard that cannot distinguish the two never protected anything.

Pure: no DB, no network, never imports main.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── the red signal: routes/dchub_media_revival.linkedin_publisher_verdict ────
@pytest.fixture()
def verdict():
    from routes.dchub_media_revival import linkedin_publisher_verdict
    return linkedin_publisher_verdict


def test_unmeasurable_publisher_is_silent_not_healthy(verdict):
    """★ THE INVERSION THAT CAUSED THE BLACKOUT. An empty snapshot means the
    read failed. 'I could not measure it' must never render as 'it is fine'."""
    out = verdict({})
    assert out["verdict"] == "silent"
    assert out["reasons"], "a red verdict must say why"


def test_the_live_outage_snapshot_is_not_healthy(verdict):
    """The exact shape of 2026-08-15: nothing published for ~72h, 8 gate
    refusals, no cards. The old logic returned healthy for this."""
    out = verdict({"hours_since_success": 72.4, "published_24h": 0,
                   "published_7d": 5, "attempts_7d": 13,
                   "with_image_7d": 0, "gate_blocked_3d": 8})
    assert out["verdict"] == "silent"
    assert out["published_7d"] == 5


def test_press_crosspost_counts_can_never_make_it_healthy(verdict):
    """★ THE WRONG-TABLE BUG, PINNED. The old component derived its verdict
    from auto_press_releases.linkedin_sent_at. Those numbers are now context
    only — a busy press path must not mask a dead publisher."""
    out = verdict({}, press_crosspost_24h=99, press_crosspost_7d=99)
    assert out["verdict"] == "silent"
    assert out["press_crosspost_7d"] == 99   # still reported, just not deciding


def test_posts_without_cards_are_degraded(verdict):
    """Publishing normally but every card dropped — the token-drift symptom."""
    out = verdict({"hours_since_success": 3.0, "published_24h": 4,
                   "published_7d": 12, "attempts_7d": 12,
                   "with_image_7d": 0, "gate_blocked_3d": 0})
    assert out["verdict"] == "degraded"
    assert any("card" in r for r in out["reasons"])


def test_gate_refusal_loop_is_degraded(verdict):
    """The deadlock's own signature: refusals piling up, nothing getting out."""
    out = verdict({"hours_since_success": 20.0, "published_24h": 0,
                   "published_7d": 9, "attempts_7d": 20,
                   "with_image_7d": 9, "gate_blocked_3d": 6})
    assert out["verdict"] == "degraded"
    assert any("refus" in r for r in out["reasons"])


def test_below_cadence_is_weak_and_healthy_is_reachable(verdict):
    """The guard must still be able to say 'fine' — a monitor that is always
    red is as useless as one that is always green."""
    weak = verdict({"hours_since_success": 5.0, "published_24h": 1,
                    "published_7d": 4, "attempts_7d": 5,
                    "with_image_7d": 4, "gate_blocked_3d": 0})
    assert weak["verdict"] == "weak"
    ok = verdict({"hours_since_success": 2.0, "published_24h": 4,
                  "published_7d": 21, "attempts_7d": 22,
                  "with_image_7d": 21, "gate_blocked_3d": 1})
    assert ok["verdict"] == "healthy"
    assert ok["reasons"] == []


# ── the feedback loop: routes/media_editorial ────────────────────────────────
_BLOCKED = {"kind": "agent_demand", "dedup_key": "agent_demand:wk202633",
            "score": 45.24, "raw_score": 34.8}
_OPEN    = {"kind": "interconnection", "dedup_key": "queue:neso",
            "score": 42.55, "raw_score": 50.0}


@pytest.fixture()
def med(monkeypatch):
    """media_editorial with every external read stubbed. Tests set
    `blocked` and `ledger` per case."""
    import routes.media_editorial as m
    monkeypatch.delenv("MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE", raising=False)
    monkeypatch.setattr(m, "_conn", lambda: None)
    monkeypatch.setattr(m, "_recently_posted_keys", lambda **k: set())
    monkeypatch.setattr(m, "_topic_mix_weights", lambda: {})
    monkeypatch.setattr(m, "_semantic_repeat_predicate",
                        lambda ranked: (lambda lead: False))
    monkeypatch.setattr(m, "recent_lead_ledger", lambda **k: [])
    monkeypatch.setattr(m, "recent_publish_blocked_keys", lambda **k: set())
    return m


def _leads(m, monkeypatch, leads):
    monkeypatch.setattr(m, "rank_data_events", lambda: [dict(x) for x in leads])


def test_blocked_lead_yields_the_slot_to_the_next_lead(med, monkeypatch):
    """★ THE DEADLOCK, DIRECTLY. The top-scoring lead is one the publish gate
    has already refused twice; the slot must go to the next eligible lead
    instead of electing the refused one for the ninth time."""
    _leads(med, monkeypatch, [_BLOCKED, _OPEN])
    monkeypatch.setattr(med, "recent_publish_blocked_keys",
                        lambda **k: {"wk202633"})
    out = med.editorial_decision()
    assert out["post"] is True
    assert out["lead"]["dedup_key"] == "queue:neso"


def test_control_without_the_block_the_refused_lead_wins(med, monkeypatch):
    """★ NEGATIVE CONTROL — this is the PRE-FIX behaviour. If this test ever
    fails, the test above stopped proving anything (it would be passing for
    some unrelated reason, e.g. the lead being filtered by another gate)."""
    _leads(med, monkeypatch, [_BLOCKED, _OPEN])
    out = med.editorial_decision()          # blocked set is empty
    assert out["lead"]["dedup_key"] == "agent_demand:wk202633"


def test_blocked_lead_is_annotated_with_why(med, monkeypatch):
    """post:false must stay a diagnosis, not a black box."""
    _leads(med, monkeypatch, [_BLOCKED, _OPEN])
    monkeypatch.setattr(med, "recent_publish_blocked_keys",
                        lambda **k: {"wk202633"})
    out = med.editorial_decision()
    ann = {l["dedup_key"]: l["_novelty"] for l in out["ranked"]}
    assert ann["agent_demand:wk202633"] == "publish_blocked:wk202633"
    assert ann["queue:neso"] == "fresh"


def test_relaxed_path_also_honors_the_block(med, monkeypatch):
    """★ EVERY PATH, NOT JUST THE STRICT ONE. When the strict filter empties
    (both kinds on cooldown), selection falls to the relaxed ladder. A path
    that skips the block is a path the deadlock returns through."""
    _leads(med, monkeypatch, [_BLOCKED, _OPEN])
    monkeypatch.setattr(med, "recent_lead_ledger", lambda **k: [
        {"kind": "agent_demand",   "entity": "", "days_ago": 0.5},
        {"kind": "interconnection", "entity": "", "days_ago": 0.5}])
    monkeypatch.setattr(med, "recent_publish_blocked_keys",
                        lambda **k: {"wk202633"})
    out = med.editorial_decision()
    assert out["post"] is True
    assert out["lead"]["dedup_key"] == "queue:neso"


def test_reserved_capability_slot_also_honors_the_block(med, monkeypatch):
    """The reserved slot deliberately bypasses the NOVELTY gates. A card the
    publisher keeps refusing is not a novelty question — it must still be
    stood down, or the deadlock survives on the reserved slot alone."""
    blocked_cap = {"kind": "cap_tool_catalog", "dedup_key": "cap:toolcatalog",
                   "score": 30.0, "raw_score": 30.0}
    open_cap    = {"kind": "data_milestone", "dedup_key": "cap:facilities",
                   "score": 20.0, "raw_score": 20.0}
    _leads(med, monkeypatch, [blocked_cap, open_cap])
    # Both entities inside the entity window, so the strict / relaxed / stale
    # ladders all come up empty and selection actually REACHES the bypass.
    # (Without this the strict filter alone answers the question and the test
    # is vacuous — it passed against a mutant that removed the bypass guard.)
    monkeypatch.setattr(med, "recent_lead_ledger", lambda **k: [
        {"kind": "other", "entity": "toolcatalog", "days_ago": 1.0},
        {"kind": "other", "entity": "facilities",  "days_ago": 1.0}])
    monkeypatch.setattr(med, "recent_publish_blocked_keys",
                        lambda **k: {"toolcatalog"})
    out = med.editorial_decision(slot="capability")
    assert out.get("reserved_slot_bypass") is True, \
        "this test must exercise the bypass, not the strict filter"
    assert out["lead"]["dedup_key"] == "cap:facilities"


def test_feedback_is_fail_open_when_disabled(med, monkeypatch):
    """A kill switch the operator can reach without a deploy — and it must
    RELAX the guard (publish anyway), never dark-hold the feed."""
    monkeypatch.setenv("MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE", "1")
    assert med.recent_publish_blocked_keys() == set()


def test_only_gate_refusals_count_not_transient_failures():
    """★ THE PREDICATE THAT MUST NOT WIDEN. `claimed_in_flight` and LinkedIn
    5xx are transient — the story is fine and deserves a retry. Counting them
    would convert a publisher outage into an editorial blackout."""
    import inspect
    from routes import media_editorial as m
    src = inspect.getsource(m.recent_publish_blocked_keys)
    assert "success = FALSE" in src
    assert "LIKE 'gate:%%'" in src, \
        "psycopg2 interpolates when args are passed — the % MUST be doubled"
    assert "HAVING COUNT(*) >= %s" in src, "one bad slot must not stand a lead down"


def test_image_upload_uses_the_same_token_as_the_post():
    """★ THE TOKEN DRIFT. The upload must not read the env var directly while
    the publish call uses the refreshed DB token — that drift is silent (the
    post still ships, just stripped of its card)."""
    import inspect
    import linkedin_poster as lp
    src = inspect.getsource(lp.post_to_linkedin)
    head = src.split("_company = os.environ.get")[0]
    assert "_get_valid_token()" in head, \
        "image upload must resolve its token the same way the post does"
    assert "LINKEDIN_IMAGE_AUTH_FAIL" in src, \
        "an auth failure here silently kills every card — it must log at ERROR"
