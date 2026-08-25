"""The lessons must actually REACH the generator (2026-08-25).

`/api/v1/media/self-critique` has returned a field named
`lessons_fed_to_generator` since r66, and its docstring promised "the exact
lessons now fed back into the generator's prompt".

★★★ NOTHING READ IT. Measured 2026-08-25: the field was built in
content_publisher, put into a jsonify, and returned. The composer's references
to "lesson", "critique", "blocked" and "rejected" numbered ZERO. 103 blocked
drafts — 47 thin, 26 duplicate, 7 value-less deal stubs — produced exactly zero
input to the thing that writes the next post.

That is why the desk read as a pipeline with bouncers rather than an analyst:
every quality improvement in this codebase's history had been a FILTER added
downstream, never a SIGNAL sent upstream.

★ THESE TESTS DRIVE THE REAL PROMPT, not the helper. One hour before this file
was written, an identical-looking suite for composer stop_reason passed while
the recording call had been deleted from the call site — it tested the helper
and proved nothing. A feedback loop must be asserted where it is CONSUMED.
"""
import json as _json
import os
import sys
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.linkedin_content_engine as ce                    # noqa: E402
from routes.media_post_quality import (                        # noqa: E402
    lessons_prompt_block, _norm_lesson)

# Verbatim from /api/v1/media/self-critique on 2026-08-25.
_LIVE_REASONS = [
    "low quality score 0.450 < 0.600 (CONTENT_QUALITY_MIN) — refusing thin/low-signal post",
    "low quality score 0.512 < 0.600 (CONTENT_QUALITY_MIN) — refusing thin/low-signal post",
    "editor rejected — Fabricated deal numbers; no verification in DC Hub M&A tracker.",
    "editor rejected — Draft cuts off mid-sentence; incomplete/broken ending line.",
    "duplicate opening hook (“Tulsa clears 75 on excess”) already posted within 5d",
    "duplicate opening hook (“Olathe posts 66.6 on excess”) already posted within 5d",
]


# ══ 1. the block itself ════════════════════════════════════════════════════
def test_variants_of_one_failure_collapse_to_one_lesson():
    """★ CONCRETE REASONS CARRY PER-POST SPECIFICS. Without normalization, 26
    near-identical duplicate-post lines crowd out every other failure mode and
    the model learns one thing instead of five."""
    a = _norm_lesson("low quality score 0.450 < 0.600 (CONTENT_QUALITY_MIN) — refusing thin post")
    b = _norm_lesson("low quality score 0.512 < 0.600 (CONTENT_QUALITY_MIN) — refusing thin post")
    assert a == b


def test_the_block_names_concrete_failures_not_category_counts():
    """"47× thin/low-signal content" tells a writer nothing. The refusal text
    does."""
    out = lessons_prompt_block(_LIVE_REASONS)
    assert "Fabricated deal numbers" in out
    assert "cuts off mid-sentence" in out
    assert "duplicate opening hook" in out


def test_the_block_is_ranked_and_bounded():
    out = lessons_prompt_block(_LIVE_REASONS, limit=2)
    assert out.count("\n  - ") == 2
    assert out.index("duplicate opening hook") < out.index("low quality score")  # 2× first


def test_no_rejections_means_no_block():
    """★ NEGATIVE CONTROL. A clean week must add nothing to the prompt, or the
    composer gets noise where a signal should be."""
    assert lessons_prompt_block([]) == ""
    assert lessons_prompt_block(["", "   ", None]) == ""


# ══ 2. THE LOOP — asserted where it is consumed ════════════════════════════
class _Capture:
    """Records the prompt actually sent to the API."""
    def __init__(self):
        self.body = None

    def __call__(self, req, *a, **k):
        self.body = _json.loads(req.data.decode("utf-8"))
        return _Resp({"content": [{"text": "Tulsa clears 75 on excess power. Verify time-to-power."}],
                      "stop_reason": "end_turn", "usage": {"output_tokens": 340}})


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
    monkeypatch.setattr(urllib.request, "urlopen", cap)
    return cap


def _prompt(cap):
    return cap.body["messages"][0]["content"]


def test_the_lessons_reach_the_actual_prompt(capture, monkeypatch):
    """★★★ THE WHOLE POINT. If this fails, `lessons_fed_to_generator` is a
    lie again."""
    monkeypatch.setattr(ce, "_recent_block_reasons", lambda *a, **k: _LIVE_REASONS)
    ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")
    p = _prompt(capture)
    assert "WHY YOUR RECENT DRAFTS WERE REFUSED" in p
    assert "Fabricated deal numbers" in p


def test_control_no_rejections_leaves_the_prompt_clean(capture, monkeypatch):
    """★ NEGATIVE CONTROL — proves the text above comes from the LESSONS and not
    from some other part of the prompt that always contains it."""
    monkeypatch.setattr(ce, "_recent_block_reasons", lambda *a, **k: [])
    ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")
    assert "WHY YOUR RECENT DRAFTS WERE REFUSED" not in _prompt(capture)


def test_a_broken_lesson_read_never_blocks_composition(capture, monkeypatch):
    """★ FAIL-OPEN. Guidance to a writer must never be able to silence a slot —
    the failure this program spent August undoing."""
    def boom(*a, **k):
        raise RuntimeError("media_review_log is gone")
    monkeypatch.setattr(ce, "_recent_block_reasons", boom)
    out = ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")
    assert out and "Tulsa" in out, "a lesson-read failure silenced the composer"


def test_the_reader_is_wired_into_the_composer_source():
    """Pins the call site itself, so deleting the injection is loud."""
    import inspect
    src = inspect.getsource(ce)
    assert "_recent_block_reasons()" in src
    assert "lessons_prompt_block" in src
