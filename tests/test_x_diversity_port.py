"""X-diversity port — 2026-07-31.

The 2026-07-17 verbatim audit measured X at 100% ONE template city-swapped
(7/7 posts, a Cheyenne repeat included) while the 07-14 diversity fix covered
only LinkedIn. The 07-31 re-measure: 22 of 27 X posts in 14d were still one
shape — the press-release distribution template — because:

  1. marketing_engine's publish-now X branch had NO gate at all (LinkedIn got
     r86c; X posted straight through _post_to_twitter every 3h — 5 tweets in
     40 seconds on 07-31, a verbatim AWS repeat 11 days apart);
  2. the X drain picked LIMIT 1 with no per-class rule, so the press template
     took both daily slots;
  3. the editorial-desk leads that DID reach the X queue died at the quality
     gate — 11 of 16 X rejections were 'quality < 0.60' on the mechanically
     shortened wire text (the greedy prefix drops the labeled stat the full
     LinkedIn post carries further down).

This file pins the port of the LinkedIn controls to X:
  - _publish_press_tweet: gate parity (r86c), X daily cap, one press tweet a
    day, terminal-reject vs defer semantics, tweet-id persistence (r-xid);
  - _x_source_class: the drain's per-source-class 1/day key;
  - _shorten_analytical: score-aware shortening judged on as_published() wire
    text — the 0728 rule: score what SHIPS, not the draft;
  - the headline regression: two consecutive composed X posts for DIFFERENT
    leads are not >80% token-identical, measured on the wire.

Pure functions + stub DB only; never imports main.
"""
import os
import sys
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402
ce = pytest.importorskip("routes.content_enqueue")  # noqa: E402
me = pytest.importorskip("routes.marketing_engine")  # noqa: E402


# ── fixtures ───────────────────────────────────────────────────────────────

# Composed analyst posts for two DIFFERENT editorial-desk leads (dcpi_build vs
# deal), shaped like the real drumbeat output the desk produced 07-25..07-31.
LI_DCPI_BUILD = (
    "Produced, then throttled: around Williston a meaningful share of "
    "generation is being curtailed because it cannot reach load. "
    "That is a transmission problem wearing a generation costume. "
    "DC Hub's excess-power index scores the market 71/100 this week, among "
    "the deepest grid headroom of the 311 markets we track, because stranded "
    "generation is real supply waiting on wires. "
    "For siting teams, curtailment is a price signal: the power is already "
    "there.\n\n"
    "Daily-refreshed score, methodology + sources on the live page: "
    "https://dchub.cloud/dcpi/williston-nd\n\n"
    "#datacenter #DCPI #mro"
)

LI_DEAL = (
    "$2.3B: an AirTrunk transaction is the largest disclosed data-center "
    "deal in DC Hub's tracker this week, one of 28 deals added to the live "
    "index in the past 24 hours alone. "
    "The pattern in every record deal this quarter is the same: capital is "
    "paying for secured power, not shells. "
    "Watch the megawatts, not the square footage.\n\n"
    "Live tracker: https://dchub.cloud/transparency\n\n"
    "#datacenter #deals"
)

# The audited pair, verbatim from production (social_media_posts 104378 and
# 104381, both published to X on 2026-07-17) — the retired city-swap template
# this port exists to kill. The similarity metric MUST flag these as the same
# post, or the regression test above it is measuring nothing.
AUDITED_WICHITA = (
    "Wichita (SPP) rates BUILD on the DC Hub Power Index this week — "
    "Excess-Power score 60/100 on our public 0-100 grid-headroom index. "
    "Real headroom, shorter interconnection timelines: a green light for AI "
    "siting.\n\nDaily score + methodology: https://dchub.cloud/dcpi/wichita"
)
AUDITED_LENEXA = (
    "Lenexa (SPP) rates BUILD on the DC Hub Power Index this week — "
    "Excess-Power score 60/100 on our public 0-100 grid-headroom index. "
    "Real headroom, shorter interconnection timelines: a green light for AI "
    "siting.\n\nDaily score + methodology: https://dchub.cloud/dcpi/lenexa"
)


def _token_similarity(a: str, b: str) -> float:
    """Share of token-level agreement between two posts — the audit's verbatim
    measure. SequenceMatcher over whitespace tokens: 1.0 = identical."""
    return SequenceMatcher(None, (a or "").split(), (b or "").split()).ratio()


def _x_wire(li_text: str) -> str:
    """What X actually receives for a composed post: the shortener's output
    hard-cut the way the poster cuts it."""
    short = ce._shorten_analytical(li_text, 275)
    assert short, "shortener returned nothing for a composed analyst post"
    return cp.as_published(short, "twitter")


# ── the headline regression ────────────────────────────────────────────────

def test_two_consecutive_x_posts_for_different_leads_are_not_token_identical():
    """THE port's acceptance test: two consecutive composed X posts for
    different editorial leads must not be >80% token-identical on the wire."""
    a = _x_wire(LI_DCPI_BUILD)
    b = _x_wire(LI_DEAL)
    sim = _token_similarity(a, b)
    assert sim <= 0.80, (
        "consecutive X posts for different leads are %.0f%% token-identical — "
        "the city-swap failure mode is back:\nA: %r\nB: %r" % (sim * 100, a, b))


def test_similarity_metric_catches_the_audited_template():
    """Control: the metric must flag the real 07-17 pair (city-swapped, both
    published to X the same day) as the same post — otherwise the test above
    proves nothing."""
    sim = _token_similarity(AUDITED_WICHITA, AUDITED_LENEXA)
    assert sim > 0.80, (
        "the audited Wichita/Lenexa pair scores %.2f — the metric no longer "
        "catches the exact failure the audit measured" % sim)


# ── score-aware shortener ──────────────────────────────────────────────────

def test_shortener_recovers_the_labeled_stat_window():
    """The greedy prefix of LI_DCPI_BUILD scores below the gate (this was 11
    of 16 X rejections); the score-aware shortener must walk to the sentence
    window carrying the labeled stat and return wire text that CLEARS it."""
    out = ce._shorten_analytical(LI_DCPI_BUILD, 275)
    assert out is not None
    wire = cp.as_published(out, "twitter")
    assert cp._quality_score(wire) >= cp.QUALITY_MIN
    assert "71/100" in wire, "the labeled-stat sentence is what should survive"
    assert "dchub.cloud" in wire, "the link must survive shortening"


def test_prefix_alone_would_fail_the_gate():
    """MUST-FAIL control for the test above: the pre-port behaviour (greedy
    first-sentences prefix + link) scores BELOW the gate on this fixture. If
    this ever passes the gate, the fixture no longer models the bug and both
    tests need a harder one."""
    import re
    url = re.search(r"https?://\S+", LI_DCPI_BUILD).group(0)
    body = re.split(r"https?://", LI_DCPI_BUILD)[0].strip()
    sents = re.split(r"(?<=[.!?])\s+", body)
    budget = 275 - (len(url) + 2)
    prefix = ""
    for s in sents:
        cand = (prefix + " " + s).strip()
        if len(cand) > budget:
            break
        prefix = cand
    old_wire = cp.as_published(prefix + "\n\n" + url, "twitter")
    assert cp._quality_score(old_wire) < cp.QUALITY_MIN, (
        "the plain prefix now clears the gate — fixture no longer exercises "
        "the score-aware window walk")


def test_shortener_returns_none_when_nothing_clears():
    """07-11 rule: never enqueue a row the publisher is guaranteed to refuse.
    A composed post with no stat, no subject and no link must yield None."""
    thin = (
        "The build window matters and teams should think carefully. "
        "There is a lot to consider when planning ahead for the future. "
        "Momentum continues and the trend is interesting to watch closely."
    )
    assert ce._shorten_analytical(thin, 275) is None


def test_shortener_fails_open_to_prefix_without_scorer(monkeypatch):
    """Scorer unavailable → the shortener must degrade to the pre-port greedy
    prefix, never to silence (fail-open contract)."""
    monkeypatch.setitem(sys.modules, "content_publisher", None)
    out = ce._shorten_analytical(LI_DCPI_BUILD, 275)
    assert out is not None
    assert out.startswith("Produced, then throttled"), (
        "fail-open output should be the old greedy prefix")
    assert len(out) <= 275


def test_shortener_bluesky_platform_wire():
    """The bluesky caller passes its own platform so the gate judges the
    297+ellipsis wire, not X's 280 cut."""
    out = ce._shorten_analytical(LI_DCPI_BUILD, 295, platform="bluesky")
    assert out is not None and len(out) <= 295


# ── the drain's per-source-class key ───────────────────────────────────────

def test_x_source_class_press_beats_lead_kind():
    """A press-distribution row is class 'press' no matter what else is
    stamped — the template shape is the class, not the story."""
    assert cp._x_source_class(123, "deal", "Google Closes $1.0B…") == "press"


def test_x_source_class_editorial_kinds_are_distinct():
    assert cp._x_source_class(None, "deal", "…") == "lead:deal"
    assert cp._x_source_class(None, "dcpi_build", "…") == "lead:dcpi_build"
    assert cp._x_source_class(None, "Agent_Demand", "…") == "lead:agent_demand"


def test_x_source_class_falls_back_to_copy_classifier():
    assert cp._x_source_class(None, None, "plain analyst text") == "other"
    assert (cp._x_source_class(None, None,
                               "The Switzerland model: an open invitation…")
            == cp._classify_post_for_dedup(
                "The Switzerland model: an open invitation…"))


def test_drain_consults_the_class_seen_set():
    """Wiring check: the Twitter drain builds and consults _seen_x_classes.
    The helper's behaviour is tested above; this pins that the loop actually
    uses it (the LinkedIn drain's _seen_classes_today shape)."""
    src = open(os.path.join(ROOT, "content_publisher.py"), encoding="utf-8").read()
    tw_loop = src.split("def start_twitter_publisher", 1)[1] \
                 .split("def start_bluesky_publisher", 1)[0]
    assert "_seen_x_classes" in tw_loop
    assert "_x_source_class" in tw_loop
    assert "press_release_id, lead_kind" in tw_loop, (
        "the candidate SELECT must carry the class columns")


# ── agent_demand + analyst-voice routing into X ────────────────────────────

def test_agent_demand_routes_into_the_x_composer_path():
    """The desk's agent_demand kind must map to the dedicated composer slot —
    this is the routing that lets 'what agents asked' lead an X post the
    moment lead supply resumes (the positive-mandate guard owns supply)."""
    assert ce._LEAD_KIND_TO_SLOT_TOPIC.get("agent_demand") == "agent_demand"


def test_every_desk_kind_maps_to_a_composer_slot():
    valid_slots = {"dcpi_mover", "hyperscaler_deal", "agent_demand",
                   "industry_pulse"}
    for kind, slot in ce._LEAD_KIND_TO_SLOT_TOPIC.items():
        assert slot in valid_slots, (kind, slot)


# ── publish-now X branch: gate parity + caps ───────────────────────────────

class _FakeCursor:
    def __init__(self, state):
        self._state = state
        self._last = ""

    def execute(self, sql, params=None):
        self._last = sql
        self._state["sql"].append((" ".join(sql.split()), params))

    def fetchone(self):
        if "SELECT status FROM social_media_posts" in self._last:
            return self._state.get("status_row", ("approved",))
        if "COUNT(press_release_id)" in self._last:
            return self._state.get("counts_row", (0, 0))
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, state):
        self._state = state

    def cursor(self):
        return _FakeCursor(self._state)

    def commit(self):
        self._state["commits"] += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def press_env(monkeypatch):
    """Stub DB + posting surface for _publish_press_tweet. Returns the shared
    state dict; tweak state['counts_row'] / gate behaviour per test."""
    state = {"sql": [], "commits": 0, "tweeted": [], "marked": [],
             "blocked": [], "gate": (False, "")}

    def _fake_tweet(text):
        state["tweeted"].append(text)
        return True, "1234567890"

    monkeypatch.setattr(me, "_conn", lambda: _FakeConn(state))
    monkeypatch.setattr(cp, "_should_skip_publish",
                        lambda cur, text, plat: state["gate"])
    monkeypatch.setattr(cp, "_post_to_twitter", _fake_tweet)
    monkeypatch.setattr(cp, "_record_media_block",
                        lambda plat, why, text="": state["blocked"]
                        .append((plat, why)))
    monkeypatch.setattr(me, "_mark_published",
                        lambda post_id, plat, tweet_id=None: state["marked"]
                        .append((post_id, plat, tweet_id)))
    return state


def _reject_updates(state):
    return [s for s, _ in state["sql"]
            if "SET status = 'rejected'" in s]


def test_press_tweet_posts_and_persists_tweet_id(press_env):
    out = me._publish_press_tweet(9, 42, "$2.3B AirTrunk deal… "
                                          "https://dchub.cloud/transparency")
    assert out["ok"] is True
    assert press_env["tweeted"], "tweet should have been sent"
    assert press_env["marked"] == [(42, "twitter", "1234567890")], (
        "r-xid: the tweet id must be persisted with the publish mark")


def test_press_tweet_gate_refusal_is_terminal_and_logged(press_env):
    press_env["gate"] = (True, "duplicate opening hook (…) already posted")
    out = me._publish_press_tweet(9, 42, "same-hook press blast")
    assert out["ok"] is False and out.get("skipped") is True
    assert not press_env["tweeted"], "a gated post must never reach X"
    assert press_env["blocked"] and press_env["blocked"][0][0] == "twitter"
    assert _reject_updates(press_env), (
        "gate refusals are content-intrinsic — the row must be terminal "
        "'rejected' (r78), not silently retried every 3h")


def test_press_tweet_defers_at_the_x_daily_cap(press_env):
    press_env["counts_row"] = (2, 0)
    out = me._publish_press_tweet(9, 42, "fine content")
    assert out.get("deferred") is True and "x_daily_cap" in out["reason"]
    assert not press_env["tweeted"]
    assert not _reject_updates(press_env), (
        "a cap hit is time-scoped, not content-intrinsic — the row must stay "
        "approved for the 6h drain")


def test_press_tweet_defers_when_a_press_tweet_already_fired_today(press_env):
    press_env["counts_row"] = (1, 1)
    out = me._publish_press_tweet(9, 42, "fine content")
    assert out.get("deferred") is True and "press_class_daily" in out["reason"]
    assert not press_env["tweeted"]
    assert not _reject_updates(press_env)


def test_press_tweet_never_reposts_a_published_row(press_env):
    press_env["status_row"] = ("published",)
    out = me._publish_press_tweet(9, 42, "already went out")
    assert out.get("skipped") is True and out["reason"] == "already_published"
    assert not press_env["tweeted"]


def test_publish_now_x_branch_routes_through_the_gated_helper():
    """The route must not grow a second ungated _post_to_twitter call — that
    asymmetry (LinkedIn r86c-gated, X raw) is the audited failure. The only
    press-path _post_to_twitter import allowed in this module is the one
    inside _publish_press_tweet, which runs cap + class + gate first."""
    src = open(os.path.join(ROOT, "routes", "marketing_engine.py"),
               encoding="utf-8").read()
    x_branch = src.split("# Twitter / X — X-diversity port", 1)[1] \
                  .split("def _publish_press_tweet", 1)[0]
    assert "_publish_press_tweet(" in x_branch
    assert "from content_publisher import _post_to_twitter" not in x_branch, (
        "the route's X branch bypasses the gated helper")
    helper = src.split("def _publish_press_tweet", 1)[1] \
                .split("def _mark_published", 1)[0]
    assert "_should_skip_publish" in helper and "_post_to_twitter" in helper
