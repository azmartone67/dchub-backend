"""Four loop-scoring guards, each pinned against the live defect it was written for.

Every one of these shipped as a number that looked HEALTHY. That is the whole
subject: none of them read as broken, so none of them were caught by looking at
a dashboard. They were caught by diffing persisted snapshots against the code
that produced them, on 2026-09-18:

    retrieval 0.833   published on a tick where 0 of the 2 queries that actually
                      ran passed their floor, because the hit-rate denominator
                      was the size of the eval SET and the 4 skipped queries
                      were therefore counted as passes. Byte-identical to the
                      prior tick's genuine 4-of-6 pass, which is why it read as
                      stable on 17 of 20 snapshots.

    eval_skipped 4    one overrun attributed the whole remaining TAIL to itself
                      and stopped, so truncation always ate the same queries.

    reviewed 0        COUNT(*) FILTER (WHERE status = 'reviewed') on a table
                      where nothing has ever written that status. Five months,
                      622 proposals, structurally unproduceable -- on the
                      endpoint whose purpose is "is brain learning?".

    growth_score 100  four components that each score full marks for merely not
                      going backwards, while the tick's own gap table read 1.6%
                      of target; and min() over the resulting all-equal dict
                      returning the FIRST key, so the manager recommended
                      raise_cadence on 20 consecutive snapshots.

Stdlib + pytest; no DB, no network.
"""
import re
import pathlib
import threading
import time

import pytest

from routes import rag_master_shell as rag
from routes import media_growth_master_shell as mg
from routes import brain_null_signal_detector as nsd

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── 1. the retrieval lever scores what it MEASURED ───────────────────────────
def _measure(measured, below, mean, zero=0):
    return {"eval_measured": measured, "eval_below_floor": below,
            "eval_mean_cosine": mean, "eval_zero_results": zero,
            "hnsw_present": True, "deep_dives_present": 300,
            "deep_dives_target": 300, "coverage_pct": 96, "rag_agents_7d": 0}


def test_all_measured_queries_failing_cannot_score_like_two_thirds_passing():
    """The live 09-18 tick: 2 of 6 ran, BOTH below floor. Pre-fix this published
    (6-2)/6 = 0.667 -> retrieval 0.833, the same value as the healthy tick
    before it. The measured hit-rate is 0 of 2, so the lever must collapse to
    the base-only floor of 0.5, not sit at 0.833."""
    truncated = rag.tier2_score_levers(_measure(measured=2, below=2, mean=0.6842))
    assert truncated["scores"]["retrieval"] == 0.5
    assert truncated["scores"]["retrieval"] != 0.833, (
        "0.833 is the pre-fix value -- the skipped queries are being counted as passes")


def test_a_full_run_with_the_same_below_count_still_scores_higher():
    """Same `below`, different denominator: 2 of 6 failing is genuinely better
    than 2 of 2 failing, and the lever has to be able to say so."""
    full = rag.tier2_score_levers(_measure(measured=6, below=2, mean=0.7681))
    truncated = rag.tier2_score_levers(_measure(measured=2, below=2, mean=0.7681))
    assert full["scores"]["retrieval"] > truncated["scores"]["retrieval"]
    assert full["scores"]["retrieval"] == 0.833   # the pre-fix value, still correct HERE


def test_pre_0918_snapshots_fall_back_to_their_per_query_rows():
    """Snapshots written before eval_measured existed still have to score. The
    per-query list is the same denominator under another name."""
    m = _measure(measured=None, below=1, mean=0.80)
    m.pop("eval_measured")
    m["eval_per_query"] = [{"q": "a"}, {"q": "b"}, {"q": "c"}, {"q": "d"}]
    assert rag.tier2_score_levers(m)["scores"]["retrieval"] == 0.875


def test_nothing_measured_is_degraded_not_a_divide_by_zero():
    m = _measure(measured=0, below=0, mean=None)
    m["eval_per_query"] = []
    assert rag.tier2_score_levers(m)["scores"]["retrieval"] == 0.2


# ── 2. the concurrent eval set ───────────────────────────────────────────────
def test_a_finished_query_still_counts_when_an_earlier_one_overran():
    """The shared deadline is the point. Query 0 hangs past the budget; query 1
    finished long before it. Positional alignment must hold and the fast result
    must survive -- the sequential loop threw away everything after the overrun."""
    def fake(q, k=8, corpus=None):
        if q == "slow":
            time.sleep(5)
            return [{"cosine": 0.9, "text": "late"}]
        return [{"cosine": 0.88, "text": "fast"}]

    specs = [{"q": "slow"}, {"q": "fast"}]
    out = rag._eval_all(fake, specs, budget=0.5)
    assert out[0] == (None, True)                  # skipped, not "broken"
    assert out[1][1] is False
    assert out[1][0][0]["text"] == "fast"


def test_one_overrun_skips_one_query_not_the_whole_tail(monkeypatch):
    """Pre-fix, a single overrun set eval_skipped to the entire remainder and
    broke -- which is why the SAME tail queries were always the missing ones."""
    import routes.brain_rag as br

    def fake(q, k=8, corpus=None):
        if q == rag._EVAL_QUERIES[0]["q"]:
            time.sleep(3)
        return [{"cosine": 0.95, "text": "virginia loudoun ashburn dominion "
                                         "southeast georgia nuclear gas pjm"}]

    monkeypatch.setattr(br, "retrieve_context", fake)
    monkeypatch.setattr(rag, "_EVAL_BUDGET_S", 0.6)
    out = rag._measure_eval(prev=None)

    assert out["eval_skipped"] == 1, out
    assert out["eval_measured"] == len(rag._EVAL_QUERIES) - 1
    assert len(out["eval_per_query"]) == out["eval_measured"]
    assert out["eval_full"] is False


# ── 3. a FILTERed status literal must have a writer ──────────────────────────
_STATUS_FILTER = re.compile(r"FILTER\s*\(\s*WHERE\s+status\s*=\s*'([a-z_]+)'", re.I)


def test_every_status_counted_on_the_effectiveness_endpoint_has_a_writer():
    """The guard that would have caught `reviewed`. A FILTER on a status literal
    that no code path ever writes is a counter pinned at zero forever, and it
    reads as a fact about the brain rather than a fact about the query."""
    src = (ROOT / "routes" / "brain_learning.py").read_text()
    counted = set(_STATUS_FILTER.findall(src))
    assert counted, "the regex found no status FILTERs -- it no longer matches the SQL"

    writers = {}
    for path in (ROOT / "routes").rglob("*.py"):
        body = path.read_text(errors="ignore")
        for status in counted:
            if re.search(r"(SET|,)\s*status\s*=\s*'%s'" % re.escape(status), body) or \
               re.search(r"status\s*=\s*'%s'\s*(,|WHERE|$)" % re.escape(status), body):
                # a write site, not the FILTER that reads it
                if "FILTER" not in body[max(0, body.find("'%s'" % status) - 60):
                                       body.find("'%s'" % status)]:
                    writers.setdefault(status, set()).add(path.name)

    unwritten = sorted(s for s in counted if s not in writers)
    assert not unwritten, (
        f"counted but never written: {unwritten} -- these can only ever report 0")


def test_the_dead_reviewed_status_is_gone_from_the_month_table():
    src = (ROOT / "routes" / "brain_learning.py").read_text()
    assert "status = 'reviewed'" not in src
    assert "pr_open_now" in src


# ── 4. the shadow-shell signal ───────────────────────────────────────────────
def test_registry_carries_the_shadow_shell_signal():
    sig = next((s for s in nsd._BOUNDED_SIGNALS
                if s["name"] == "rag_shell_actions_taken"), None)
    assert sig is not None, "signal #7 missing from _BOUNDED_SIGNALS"
    assert sig["boundary"] == "low"
    assert sig["table"] == "rag_snapshots"
    # every entry must return exactly (hits, total) -- the evaluator unpacks two
    assert sig["sql"].upper().count("COUNT(") == 2


def test_every_registry_signal_names_a_table_its_sql_reads():
    """A signal pointed at the wrong table is the #4749 defect all over again."""
    for s in nsd._BOUNDED_SIGNALS:
        assert s["table"] in s["sql"], f"{s['name']} does not read {s['table']}"


# ── 5. media growth: distance counts, and a tie is not a lever ───────────────
def _mg_measure(posts=14, eng=0.05, cv=4.0, li=373, x=4, wow=1, trend=0.0):
    return {"posts_7d": posts, "li_eng_rate": eng, "citation_velocity": cv,
            "li_followers": li, "x_followers": x, "li_followers_wow": wow,
            "citation_trend": trend}


def test_perfect_momentum_cannot_score_100_while_goals_sit_at_a_sixth():
    """The live 09-18 tick. All four momentum components at full marks, and the
    shell's own gap table reading X 4/250 and citations 4/25."""
    sc = mg.tier2_score(_mg_measure())
    assert all(sc["components"][k] == 1.0 for k in
               ("cadence", "reach", "citation_momentum", "follower_momentum"))
    assert sc["growth_score"] < 100.0, "distance to target is not in the score"
    assert sc["weakest_lever"] == "goal_progress"
    assert sc["goal_progress"] < 0.5


def test_a_goal_gap_is_not_answered_with_raise_cadence():
    """Posting more of the same at an already-maxed cadence does not close a
    1.6%-of-target follower gap, and the rationale must not claim cadence is
    below its floor when it scored 1.0."""
    m = _mg_measure()
    rec = mg.tier3_act(m, mg.tier2_score(m))
    assert rec["action"] != "raise_cadence"
    assert rec["action"] == "close_widest_goal_gap"
    assert "below the 2/day floor" not in rec["rationale"]


def test_nothing_weak_is_a_hold_not_an_invented_lever():
    """Every component at full marks AND every goal met: min() used to return
    'cadence' by dict order and the desk was told to post more."""
    sc = mg.tier2_score(_mg_measure(li=500, x=500, cv=50))
    assert sc["weakest_lever"] is None
    assert sc["growth_score"] == 100.0
    rec = mg.tier3_act(_mg_measure(li=500, x=500, cv=50), sc)
    assert rec["action"] == "hold"
    assert rec["lever"] == "none"


def test_a_genuinely_weak_lever_still_wins_over_goal_progress():
    """The fix must not make goal_progress swallow every verdict."""
    sc = mg.tier2_score(_mg_measure(posts=2, li=500, x=500, cv=50))
    assert sc["components"]["cadence"] < 1.0
    assert sc["weakest_lever"] == "cadence"


def test_unreadable_goals_do_not_read_as_failure_to_grow():
    """A telemetry blackout is audience_blind, not a zero. The weights
    renormalise so the remaining components still say what they said."""
    sc = mg.tier2_score(_mg_measure(li=None, x=None, cv=None))
    assert sc["goal_progress"] is None
    assert "goal_progress" not in sc["components"]
    assert sc["audience_blind"] is True
