"""The THIRD media loop: a PUBLISHED post gets graded, and the grade comes back.

Two of the desk's three feedback loops existed before 2026-08-25:

    what to talk about            LIVE   eng_rate/eng_weight bandit
    how to write                  LIVE   refusals -> composer prompt (#3167)
    is a PUBLISHED post any good  ABSENT

★ Why engagement is not the third grade — measured 2026-08-25 on
  /api/v1/brain/media/linkedin-engagement-scoreboard (45d, 141 posts) and
  /api/v1/brain/claims:
    - the claim bar is floor(0.5 x 30d avg impressions) ~= 17 and the WORST
      kind averages 18.3 impressions, so all nine kinds clear it on average;
    - the claim grades impressions while the bandit grades eng_rate, and the
      two are anti-correlated across kinds (Pearson r = -0.16);
    - the whole desk earns 0.5-3.0 interactions per post, so the gap between
      an excellent post and a mediocre one is under one click.

★★★ THESE TESTS DRIVE THE REAL PROMPT, not the helper. Twice on 2026-08-25 a
suite passed green while the wiring under it had been deleted. A feedback loop
must be asserted where it is CONSUMED.
"""
import json as _json
import os
import sys
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.linkedin_content_engine as ce                       # noqa: E402
import routes.media_published_review as pr                        # noqa: E402
from routes.media_post_quality import (                           # noqa: E402
    PUBLISHED_REVIEW_DIMENSIONS, published_critique_block)

# Shaped like real reviewer output against the two posts that carried claims
# on 2026-08-25 (ids 106973 / 107026), both of which announced their
# implication under the label the voice spec forbids.
_LIVE_CRITIQUES = [
    ("implication", "announced the implication under the label 'Second-order read:' instead of writing it into the prose"),
    ("implication", "announced the implication under the label 'The second-order read is' instead of writing it into the prose"),
    ("number_lead", "opened with 'DC Hub shipped memory' rather than a metric and its trend"),
    ("attribution", "put the source line before the insight rather than after it"),
    ("promotion", "closed on a brand-pillar sentence about being the standing ledger"),
]


# ══ 1. the block ═══════════════════════════════════════════════════════════
def test_the_block_names_concrete_misses_not_scores():
    """"3 of 5 posts missed on implication" teaches nothing. The critique does."""
    out = published_critique_block(_LIVE_CRITIQUES)
    assert "HOW YOUR PUBLISHED POSTS SCORED" in out
    assert "rather than a metric" in out
    assert "put the source line before the insight" in out
    assert "brand-pillar sentence" in out


def test_variants_of_one_miss_collapse_to_one_lesson():
    """★ Critiques carry per-post quotes ('Second-order read:' vs 'The
    second-order read is'). _norm_lesson strips the quoted fragment so ONE
    failure class occupies ONE slot — otherwise five phrasings of the same
    miss crowd out the other four dimensions, and the composer learns one
    thing instead of four."""
    out = published_critique_block(_LIVE_CRITIQUES)
    impl = [l for l in out.splitlines() if "implication:" in l]
    assert len(impl) == 1, f"two phrasings of one miss took two slots: {impl}"
    assert "(2×)" in impl[0]
    assert "Second-order read" not in out, \
        "a per-post quote survived normalization and will be learned as a rule"


def test_repeated_misses_rank_first():
    out = published_critique_block(_LIVE_CRITIQUES)
    assert out.index("implication:") < out.index("promotion:"), \
        "the 2x miss must outrank the 1x misses"


def test_clean_week_adds_nothing():
    """★ NEGATIVE CONTROL. A desk with nothing to learn must send no block."""
    assert published_critique_block([]) == ""
    assert published_critique_block([("implication", "  ")]) == ""


def test_an_invented_dimension_is_dropped_not_obeyed():
    """★★★ THE REVIEWER MAY NOT WRITE POLICY. The composer obeys this block, so
    a hallucinated rule would quietly become the voice spec. Only the seven
    named dimensions survive."""
    out = published_critique_block([
        ("use_more_emoji", "add emoji to increase reach"),
        ("tone", "be more enthusiastic about the brand"),
    ])
    assert out == "", "an invented dimension reached the composer's prompt"


def test_a_real_dimension_alongside_an_invented_one_still_lands():
    """★ The drop must be per-critique, not per-batch — one bad verdict must
    not discard the whole review."""
    out = published_critique_block([
        ("use_more_emoji", "add emoji to increase reach"),
        ("ending", "stopped mid-sentence after 'which means the'"),
    ])
    assert "stopped mid-sentence" in out
    assert "emoji" not in out


def test_every_named_dimension_is_accepted():
    """Guards the two lists against drifting apart: the reviewer's system
    prompt names seven dimensions and the filter must accept all seven."""
    for dim in PUBLISHED_REVIEW_DIMENSIONS:
        out = published_critique_block([(dim, "a concrete miss worth teaching")])
        assert out, f"dimension {dim!r} is documented but filtered out"


def test_the_reviewer_prompt_and_the_filter_name_the_same_dimensions():
    """★ The system prompt lists the legal dimensions in prose; the filter
    enforces them as a tuple. If they drift, the model is told to use a
    dimension every critique of which is then silently dropped."""
    for dim in PUBLISHED_REVIEW_DIMENSIONS:
        assert dim in pr._REVIEW_SYSTEM, \
            f"{dim!r} is enforced by the filter but never offered to the reviewer"


# ══ 2. THE LOOP — asserted where it is consumed ════════════════════════════
class _Capture:
    def __init__(self):
        self.body = None

    def __call__(self, req, *a, **k):
        self.body = _json.loads(req.data.decode("utf-8"))
        return _Resp({"content": [{"text": "Midland-Odessa clears 71 on excess power."}],
                      "stop_reason": "end_turn", "usage": {"output_tokens": 300}})


class _Resp:
    def __init__(self, payload): self._b = _json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture()
def capture(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ce, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(ce, "_build_user_prompt", lambda *a, **k: "write a post", raising=False)
    monkeypatch.setattr(ce, "_recent_post_openings", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ce, "_recent_block_reasons", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", cap)
    return cap


def _prompt(cap):
    return cap.body["messages"][0]["content"]


def test_the_critiques_reach_the_actual_prompt(capture, monkeypatch):
    """★★★ THE WHOLE POINT. If this fails, the review is a report, not a loop."""
    monkeypatch.setattr(pr, "recent_published_critiques", lambda *a, **k: _LIVE_CRITIQUES)
    ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")
    p = _prompt(capture)
    assert "HOW YOUR PUBLISHED POSTS SCORED" in p
    assert "rather than a metric" in p


def test_control_no_critiques_leaves_the_prompt_clean(capture, monkeypatch):
    """★ NEGATIVE CONTROL — proves the header above comes from the REVIEW and
    not from some other part of the prompt that always contains it."""
    monkeypatch.setattr(pr, "recent_published_critiques", lambda *a, **k: [])
    ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")
    assert "HOW YOUR PUBLISHED POSTS SCORED" not in _prompt(capture)


def test_the_two_loops_do_not_collide(capture, monkeypatch):
    """Refusals and published-post reviews are different lessons from different
    rows. Both must land, under their own headers."""
    monkeypatch.setattr(ce, "_recent_block_reasons", lambda *a, **k: [
        "editor rejected — Fabricated deal numbers; no verification in tracker."])
    monkeypatch.setattr(pr, "recent_published_critiques", lambda *a, **k: _LIVE_CRITIQUES)
    ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")
    p = _prompt(capture)
    assert "WHY YOUR RECENT DRAFTS WERE REFUSED" in p
    assert "HOW YOUR PUBLISHED POSTS SCORED" in p
    assert "Fabricated deal numbers" in p
    assert "rather than a metric" in p


def test_a_broken_review_read_never_blocks_composition(capture, monkeypatch):
    """★ FAIL-OPEN. This is advisory. A review-store outage must cost a hint,
    never a slot — silence is what August was spent undoing."""
    def boom(*a, **k):
        raise RuntimeError("media_review_log is gone")
    monkeypatch.setattr(pr, "recent_published_critiques", boom)
    out = ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")
    assert out and "Midland" in out, "a review-read failure silenced the composer"


def test_the_reader_is_wired_into_the_composer_source():
    """Pins the call site itself, so deleting the injection is loud."""
    import inspect
    src = inspect.getsource(ce)
    assert "recent_published_critiques()" in src
    assert "published_critique_block" in src


# ══ 3. the review pass ═════════════════════════════════════════════════════
def test_the_review_never_gates_publication():
    """★★★ THE ARCHITECTURAL PROMISE. This module runs AFTER publication. If it
    ever grows a refusal path it stops being a signal and becomes the eighth
    filter — the pattern this change exists to break."""
    import ast
    tree = ast.parse(open(pr.__file__).read())
    # Names CALLED or IMPORTED by the reviewer — docstrings and comments are
    # not code and must not trip this, or the guard becomes unwritable prose.
    reached = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            reached.add(getattr(f, "id", None) or getattr(f, "attr", None))
        elif isinstance(n, ast.ImportFrom):
            reached.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            reached.update(a.name for a in n.names)
    for banned in ("_should_skip_publish", "_post_to_linkedin",
                   "_record_media_block", "_li_gate_refusal"):
        assert banned not in reached, \
            f"{banned!r} is reachable from the reviewer — this must never stop a post"
    # And it must never write a row the refusals loop would read as a refusal.
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    docstrings = {ast.get_docstring(n) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))}
    assert "blocked" not in (literals - docstrings), \
        "the reviewer builds a 'blocked' literal — those rows feed the refusals loop"


def test_the_review_decision_is_distinct_from_blocked():
    """The refusals loop filters decision='blocked'. If these rows shared that
    value, published-post reviews would be read as pre-publish refusals and the
    reject_rate on /api/v1/media/self-critique would silently inflate."""
    assert pr.REVIEW_DECISION != "blocked"
    src = inspect.getsource(ce._recent_block_reasons)
    assert "decision = 'blocked'" in src


def test_a_truncated_review_is_discarded_not_recorded(monkeypatch):
    """★ The #3166 lesson applied here: a review CUT OFF at the ceiling has
    reviewed only the posts it got to. Recording it marks the rest clean.

    ★★★ THE PAYLOAD IS DELIBERATELY VALID JSON. The first version of this test
    used truncated-and-therefore-unparseable JSON, so json.loads() failed and
    the test passed even with the stop_reason check DELETED — it proved the
    JSON guard, not the truncation guard. Mutation testing caught it. A cut-off
    response whose prefix happens to parse is the case that matters: the model
    reviewed post 1, was cut before post 2, and the array closes anyway."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    payload = '[{"post_id": 1, "misses": [{"dimension": "hook", "critique": "buried the number"}]}]'

    class _Trunc:
        def __init__(self, stop): self.stop = stop
        def __call__(self, req, *a, **k):
            return _Resp({"content": [{"text": payload}], "stop_reason": self.stop})

    # POSITIVE CONTROL: the identical payload IS accepted when the model
    # finished — so the rejection below can only come from stop_reason.
    monkeypatch.setattr(urllib.request, "urlopen", _Trunc("end_turn"))
    assert pr._call_model([{"id": 1, "content": "x"}], "m"), \
        "a complete review was discarded — the guard is over-broad"

    monkeypatch.setattr(urllib.request, "urlopen", _Trunc("max_tokens"))
    assert pr._call_model([{"id": 1, "content": "x"}], "m") == [], \
        "a review cut off at the ceiling was recorded as if it were complete"


def test_a_verdict_for_a_post_we_did_not_send_is_ignored(monkeypatch):
    """★ The model can return an id that was never in the batch. Recording it
    would attach a critique to an unrelated post."""
    monkeypatch.setattr(pr, "recent_published_posts",
                        lambda **k: [{"id": 11, "content": "a post"}])
    monkeypatch.setattr(pr, "_call_model", lambda posts, model: [
        {"post_id": 99, "misses": [{"dimension": "hook", "critique": "buried the number"}]}])
    written = []
    monkeypatch.setattr(pr, "_record_review",
                        lambda pid, d, c: written.append((pid, d, c)) or True)
    out = pr.review_published_posts()
    assert out["reviewed"] == 0
    assert not any(w[0] == 99 for w in written)


def test_an_invented_dimension_is_counted_and_not_written(monkeypatch):
    monkeypatch.setattr(pr, "recent_published_posts",
                        lambda **k: [{"id": 11, "content": "a post"}])
    monkeypatch.setattr(pr, "_call_model", lambda posts, model: [
        {"post_id": 11, "misses": [
            {"dimension": "use_more_emoji", "critique": "add emoji"},
            {"dimension": "hook", "critique": "buried the number past word 30"}]}])
    written = []
    monkeypatch.setattr(pr, "_record_review",
                        lambda pid, d, c: written.append((pid, d, c)) or True)
    out = pr.review_published_posts()
    assert out["dropped_unknown_dimension"] == 1
    assert [w[1] for w in written if w[1] != "clean"] == ["hook"]


def test_the_pass_never_raises(monkeypatch):
    """★ FAIL-OPEN at the top level: the caller is a cron tick."""
    def boom(**k):
        raise RuntimeError("DB is gone")
    monkeypatch.setattr(pr, "recent_published_posts", boom)
    out = pr.review_published_posts()
    assert out["ok"] is False and "error" in out


def test_main_registers_the_blueprint():
    """AST, not a grep: a module nobody registers serves nothing."""
    import ast
    tree = ast.parse(open(os.path.join(ROOT, "main.py")).read())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "register_media_published_review" in called, \
        "main.py never calls register_media_published_review(app)"


import inspect  # noqa: E402  (used by the source-pinning tests above)
