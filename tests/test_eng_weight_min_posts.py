"""A kind must EARN its engagement weight before the bandit believes it.

MEASURED 2026-08-25 — 45 days, 141 LinkedIn posts. Three of the nine kinds
carried a non-neutral weight built on n<=3 posts. A jackknife (recompute the
pooled eng_rate dropping ONE post) says what those weights are made of:

    kind                 n   eng_rate  weight   drop-one swing
    new_facility         1     0.0769   1.089   the kind IS one post
    dcpi_mover           2     0.0270   0.836   137% of its own rate (to 0.0000)
    platform_milestone   3     0.0448   0.926   115% of its own rate (to 0.0000)
    agent_demand         7     0.1187   1.300    36%
    deal                34     0.0247   0.825    32%

★★★ AND THE NOISE DID NOT STAY IN ITS OWN LANE. `hi` — the top rate — is the
DENOMINATOR for every kind's weight, so a fluke in a thin lane rescales all
nine at once. One interaction on a 3-impression post is an eng_rate of 0.3333,
nearly 3x the current ceiling. Under the old code that single post would have
done this to lanes that had earned their numbers over dozens of posts:

    agent_demand  1.300 -> 0.914      deal  0.825 -> 0.744
    dcpi_build    0.855 -> 0.755

That is why the threshold gates which kinds may SET THE CEILING as well as
which kinds receive a weight. Gating only the second half would have left the
contamination fully in place — and is the easy half to ship by accident.

Under-sampled kinds are omitted from the map, which is its existing "untried
kinds stay neutral 1.0x" semantics: they keep getting explored and accumulate
the posts that would earn them a real weight.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.media_editorial as me                               # noqa: E402

# The live board on 2026-08-25, used as the fixture so these tests double as
# the record of what the change actually did to production selection.
LIVE = {
    "agent_demand":       {"eng_rate": 0.1187, "avg_impr": 22.9, "posts": 7},
    "new_facility":       {"eng_rate": 0.0769, "avg_impr": 39.0, "posts": 1},
    "platform_milestone": {"eng_rate": 0.0448, "avg_impr": 44.7, "posts": 3},
    "interconnection":    {"eng_rate": 0.0320, "avg_impr": 21.9, "posts": 10},
    "dcpi_build":         {"eng_rate": 0.0307, "avg_impr": 18.3, "posts": 32},
    "dcpi_mover":         {"eng_rate": 0.0270, "avg_impr": 18.5, "posts": 2},
    "other":              {"eng_rate": 0.0258, "avg_impr": 40.5, "posts": 42},
    "tenant":             {"eng_rate": 0.0256, "avg_impr": 39.1, "posts": 10},
    "deal":               {"eng_rate": 0.0247, "avg_impr": 46.5, "posts": 34},
}
THIN = ("new_facility", "dcpi_mover", "platform_milestone")       # n = 1, 2, 3
SEASONED = ("agent_demand", "interconnection", "dcpi_build", "other",
            "tenant", "deal")                                     # n >= 7


def test_an_undersampled_kind_gets_no_learned_weight():
    w = me._engagement_weights(LIVE)
    for k in THIN:
        assert k not in w, (
            f"{k} (n={LIVE[k]['posts']}) carries a learned weight; dropping "
            "one of its posts swings its own rate by more than 100%")


def test_a_well_sampled_kind_still_gets_one():
    """★ POSITIVE CONTROL. A threshold that neutralised EVERYTHING would pass
    the test above and silently switch the bandit off."""
    w = me._engagement_weights(LIVE)
    for k in SEASONED:
        assert k in w, f"{k} (n={LIVE[k]['posts']}) lost its learned weight"
    assert w["agent_demand"] == 1.3 and w["deal"] == 0.825, (
        "the weights the well-sampled lanes had earned must not move — this "
        f"change is about who is IN the map, not the formula: {w}")


def test_a_thin_lane_cannot_set_the_ceiling_for_everyone_else():
    """★★★ THE HALF THAT IS EASY TO MISS. `hi` is the denominator for every
    kind, so gating only the thin lane's own weight leaves it free to rescale
    all nine. One interaction on a 3-impression post is eng_rate 0.3333."""
    baseline = me._engagement_weights(LIVE)
    fluke = dict(LIVE)
    fluke["new_facility"] = {"eng_rate": 0.3333, "avg_impr": 3.0, "posts": 1}
    after = me._engagement_weights(fluke)
    assert after == baseline, (
        "a single lucky low-impression post moved the weights of lanes with "
        f"dozens of posts behind them:\n  before {baseline}\n  after  {after}")
    # And state the damage the old code would have taken, so a future reader
    # sees the size of it rather than trusting the word "contamination".
    old_hi = 0.3333
    assert round(0.7 + 0.6 * (LIVE["agent_demand"]["eng_rate"] / old_hi), 3) == 0.914
    assert after["agent_demand"] == 1.3


def test_the_threshold_is_tunable_without_a_deploy(monkeypatch):
    monkeypatch.setenv("MEDIA_ENG_WEIGHT_MIN_POSTS", "11")
    w = me._engagement_weights(LIVE)
    assert set(w) == {"dcpi_build", "other", "deal"}, (
        f"n>=11 should leave exactly the three deepest lanes, got {sorted(w)}")
    # ★ hi moved with the threshold: agent_demand no longer qualifies, so the
    #   ceiling is now dcpi_build's rate and the survivors rescale against it.
    assert w["dcpi_build"] == 1.3, \
        "the ceiling did not move to the best REMAINING kind"


def test_a_junk_threshold_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("MEDIA_ENG_WEIGHT_MIN_POSTS", "not-a-number")
    assert me._eng_weight_min_posts() == me._ENG_WEIGHT_MIN_POSTS_DEFAULT
    assert me._engagement_weights(LIVE)     # still produces a map


def test_nothing_seasoned_means_everything_neutral(monkeypatch):
    """★ FAIL-OPEN. A brand-new desk, or one whose threshold is set above its
    deepest lane, must explore everything — never starve every kind to the
    0.7 floor, and never raise."""
    monkeypatch.setenv("MEDIA_ENG_WEIGHT_MIN_POSTS", "9999")
    assert me._engagement_weights(LIVE) == {}
    assert me._engagement_weights({}) == {}


def test_the_soft_greedy_contract_is_unchanged():
    """★ The floor exists so a chronically-flopping angle is never starved to
    zero. Neutralising thin lanes must not have altered that."""
    w = me._engagement_weights(LIVE)
    assert all(0.7 <= v <= 1.3 for v in w.values()), w
    assert max(w.values()) == 1.3, "the best kind should sit at the ceiling"


def test_the_scoreboard_says_WHY_a_kind_is_neutral(monkeypatch):
    """★ A neutral 1.0 has two very different causes — 'too thin to trust yet'
    and 'genuinely scored neutral' — and an operator tuning the desk off this
    board must be able to tell them apart."""
    from flask import Flask
    monkeypatch.setattr(me, "engagement_by_kind", lambda *a, **k: LIVE)
    app = Flask(__name__)
    app.register_blueprint(me.media_editorial_bp)
    with app.test_client() as c:
        d = c.get("/api/v1/brain/media/linkedin-engagement-scoreboard").get_json()
    assert d["ok"] and d["min_posts_for_a_learned_weight"] == 5
    basis = {r["kind"]: r["weight_basis"] for r in d["by_kind"]}
    for k in THIN:
        assert basis[k].startswith("neutral: n<"), \
            f"{k} renders as an ordinary neutral score, not as under-sampled"
        assert [r for r in d["by_kind"] if r["kind"] == k][0]["score_weight"] == 1.0
    for k in SEASONED:
        assert basis[k] == "learned", f"{k} should report a learned weight"
