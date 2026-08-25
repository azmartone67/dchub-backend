"""The composer must know whether the model FINISHED (2026-08-25).

WHY THIS EXISTS. `stop_reason` appeared ZERO times in
routes/linkedin_content_engine.py before this commit. Every Anthropic response
says, on every call, whether the model ran to a natural end or was CUT OFF at
max_tokens — and the composer discarded that and handed the severed text to the
publish gate.

That is why truncation keeps coming back. It has been fixed three times AT THE
GATE and never at the source:

  2026-07-15  max_tokens 1200 -> 1800, after posts clipped mid-word
  #3153       a broken-copy publish gate
  #3162       stopping that gate from eating headlines

None of them asked the question the response already answered.

Measured live 2026-08-25T08:21:50Z: the deal/nvidia slot composed a 1,328-char
draft and TWO independent judges called it cut off — the LLM editor ("Draft cuts
off mid-sentence") and the broken-copy gate. 1,328 chars is ~340 tokens against
an 1,800 ceiling, so the cap is almost certainly NOT the cause — but with
nothing recording stop_reason, "almost certainly" was the strongest statement
anyone could make. That is the gap.

Pure: no DB, no network, no Flask app.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.linkedin_content_engine as ce  # noqa: E402


@pytest.fixture(autouse=True)
def _clear():
    del ce._COMPOSE_STOPS[:]
    yield
    del ce._COMPOSE_STOPS[:]


def test_the_source_actually_reads_stop_reason():
    """★ THE REGRESSION CONTROL, stated as the fact that was missing. If this
    ever returns to zero, the composer has gone blind again."""
    import inspect
    src = inspect.getsource(ce)
    assert "stop_reason" in src, "the composer stopped reading stop_reason"
    assert 'payload.get("stop_reason")' in src


def test_a_ceiling_hit_is_recorded_and_refused():
    ce._record_compose_stop("claude-fable-5", "max_tokens", 1800, 7100)
    snap = ce.composer_stops_snapshot()
    assert snap["truncated_at_ceiling"] == 1
    assert snap["by_stop_reason"]["max_tokens"] == 1


def test_a_normal_finish_is_recorded_too():
    """★ A STAT KEPT ONLY ON FAILURE CANNOT SHOW A RATE. Both paths record, or
    truncation_rate is meaningless."""
    ce._record_compose_stop("claude-fable-5", "end_turn", 340, 1328)
    snap = ce.composer_stops_snapshot()
    assert snap["sampled"] == 1
    assert snap["truncated_at_ceiling"] == 0
    assert snap["truncation_rate"] == 0.0


def test_the_rate_is_a_rate():
    for _ in range(3):
        ce._record_compose_stop("m", "end_turn", 300, 1200)
    ce._record_compose_stop("m", "max_tokens", 1800, 7100)
    assert ce.composer_stops_snapshot()["truncation_rate"] == 0.25


def test_an_empty_sample_is_not_a_zero_rate():
    """★ THE FLATTERING-ZERO TRAP this codebase keeps rediscovering. No data is
    not the same as no truncation, and the payload must say so."""
    snap = ce.composer_stops_snapshot()
    assert snap["sampled"] == 0
    assert snap["truncation_rate"] is None, "an empty sample rendered as a rate"
    assert "not the same as a truncation rate of zero" in snap["note"]


def test_the_buffer_is_bounded():
    for i in range(200):
        ce._record_compose_stop("m", "end_turn", 300, 1200)
    assert len(ce._COMPOSE_STOPS) <= 50


def test_recording_never_raises():
    """Telemetry must never be able to fail a compose."""
    ce._record_compose_stop(None, None, None, None)
    ce._record_compose_stop("m", object(), "x", -1)
    assert ce.composer_stops_snapshot()["ok"] is True


def test_snapshot_carries_no_post_text():
    """The buffer records SHAPE, not content — no draft text leaks into a
    public, unauthenticated endpoint."""
    ce._record_compose_stop("claude-fable-5", "end_turn", 340, 1328)
    row = ce.composer_stops_snapshot()["recent"][0]
    assert set(row) == {"model", "stop_reason", "output_tokens", "chars"}


# ══ the CALL SITE, not just the recorder ═══════════════════════════════════
# ★ MUTATION-DRIVEN. The tests above call _record_compose_stop directly, so
#   deleting the recording call from _compose_with_claude's success path left
#   all eight of them GREEN — verified by mutation. A guard that tests the
#   helper instead of the wiring proves the helper works and nothing else.
import io                                    # noqa: E402
import json as _json                         # noqa: E402
import urllib.request                        # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._b = _json.dumps(payload).encode()
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _drive(monkeypatch, payload):
    """Run the real _compose_with_claude against a stubbed API response."""
    monkeypatch.setattr(ce, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(ce, "_build_user_prompt",
                        lambda *a, **k: "write a post", raising=False)
    monkeypatch.setattr(ce, "_recent_post_openings", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(payload))
    return ce._compose_with_claude("dcpi_scoop", {}, "https://dchub.cloud/dcpi")


def test_the_success_path_records_at_the_call_site(monkeypatch):
    """★ THE MUTATION THAT SLIPPED THROUGH. If the recorder call is deleted
    from the success path, truncation_rate silently becomes 1.0-or-nothing."""
    out = _drive(monkeypatch, {
        "content": [{"text": "Tulsa clears 75 on excess power. Verify time-to-power."}],
        "stop_reason": "end_turn",
        "usage": {"output_tokens": 340},
    })
    assert out and "Tulsa" in out
    snap = ce.composer_stops_snapshot()
    assert snap["sampled"] == 1, "the success path did not record its stop_reason"
    assert snap["by_stop_reason"]["end_turn"] == 1


def test_a_ceiling_hit_is_refused_at_the_call_site(monkeypatch):
    """A known-severed draft must not reach the publish gate at all."""
    out = _drive(monkeypatch, {
        "content": [{"text": "Tulsa clears 75 on excess power and the"}],
        "stop_reason": "max_tokens",
        "usage": {"output_tokens": 1800},
    })
    assert out is None, "a max_tokens draft was handed downstream"
    snap = ce.composer_stops_snapshot()
    assert snap["truncated_at_ceiling"] == 1
    assert snap["truncation_rate"] == 1.0
