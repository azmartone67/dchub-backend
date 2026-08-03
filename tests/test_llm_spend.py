"""Durable LLM spend ledger (2026-08-02).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

"Where are the tokens going?" had no answer: register_claude_call_start/end
tracks in-flight calls in a process-local dict that survives neither a restart
nor a second worker, and records no token counts. So "reduce token spend" had
no baseline, and any change would have been judged on whether it felt cheaper.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_tokens_are_read_from_the_usage_block():
    from routes.brain_llm_spend import tokens_from_usage
    assert tokens_from_usage(
        {"usage": {"input_tokens": 1200, "output_tokens": 350}}) == (1200, 350)


def test_a_missing_usage_block_is_zero_measured_not_a_crash():
    """The row still records that the call HAPPENED, which is the difference
    between 'cheap' and 'unmeasured'."""
    from routes.brain_llm_spend import tokens_from_usage
    assert tokens_from_usage({}) == (0, 0)
    assert tokens_from_usage(None) == (0, 0)
    assert tokens_from_usage({"usage": "nonsense"}) == (0, 0)
    assert tokens_from_usage({"usage": {"input_tokens": -5}}) == (0, 0)


def test_recording_can_never_fail_the_thing_it_measures(monkeypatch):
    from routes import brain_llm_spend as s
    monkeypatch.setattr(s, "_conn", lambda: None)
    assert s.record("L14", model="m", body={"usage": {"input_tokens": 5}}) is False


def test_kill_switch(monkeypatch):
    from routes import brain_llm_spend as s
    monkeypatch.setenv("BRAIN_LLM_SPEND_DISABLE", "1")
    assert s.record("L14") is False


def test_no_price_table_lives_in_this_repo():
    """★A stale price table turns a measurement into a confident guess — the
    exact failure mode of the count-vs-duration misread. Tokens only."""
    src = _src("routes", "brain_llm_spend.py")
    low = src.lower()
    for word in ("usd", "$", "cost_per", "price_per", "per_million"):
        assert word not in low, f"a price signal ({word}) crept into the ledger"


def test_the_wrapper_returns_the_response_untouched(monkeypatch):
    """★It wraps 20+ call sites whose response handling this module knows
    nothing about, so it must be invisible."""
    from routes import brain_llm_spend as s
    import requests
    sentinel = type("R", (), {"status_code": 200, "json": lambda self: {}})()
    monkeypatch.setattr(requests, "post", lambda url, **k: sentinel)
    monkeypatch.setattr(s, "record", lambda *a, **k: True)
    assert s.instrumented_post("L", "http://x") is sentinel


def test_the_wrapper_reraises_what_requests_raises(monkeypatch):
    """★A caller that catches requests.Timeout must go on catching it.
    Swallowing here would silently change 20 modules' control flow at once."""
    from routes import brain_llm_spend as s
    import pytest, requests

    def _boom(url, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(s, "record", lambda *a, **k: True)
    with pytest.raises(requests.Timeout):
        s.instrumented_post("L", "http://x")


def test_a_broken_ledger_cannot_break_the_call(monkeypatch):
    from routes import brain_llm_spend as s
    import requests
    sentinel = type("R", (), {"status_code": 200, "json": lambda self: {}})()
    monkeypatch.setattr(requests, "post", lambda url, **k: sentinel)

    def _boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(s, "record", _boom)
    assert s.instrumented_post("L", "http://x") is sentinel


def test_model_is_read_from_the_payload():
    from routes.brain_llm_spend import _model_of
    assert _model_of({"json": {"model": "claude-sonnet-4-5"}}) == "claude-sonnet-4-5"
    assert _model_of({}) == ""
    assert _model_of({"json": "nonsense"}) == ""


def test_coverage_counts_both_call_shapes():
    """★The denominator originally keyed on `requests.post`, and migrating 20
    modules to the wrapper deleted that string from them — coverage briefly
    read "20 instrumented of 7 modules", a nonsense ratio produced by a metric
    that measured the thing being changed."""
    from routes.brain_llm_spend import instrumented_modules, count_llm_modules
    n, total = len(instrumented_modules()), count_llm_modules()
    assert total is not None and total >= n > 0, f"{n} of {total}"


def test_neither_side_of_coverage_is_hand_kept():
    """Both are scanned. A roster a human must remember to update is the same
    shape as the VALUE_NOT_COUNT_ISSUES tuple this repo edited three times
    after the fact."""
    src = _src("routes", "brain_llm_spend.py")
    assert "INSTRUMENTED = (" not in src


def test_summary_states_its_own_coverage():
    """★Per-layer totals alone would read as 'L14 is all of our spend' when it
    only means 'L14 is all of our instrumentation'."""
    from routes.brain_llm_spend import summary

    class _Cur:
        def execute(self, *a, **k):
            return None

        def fetchall(self):
            return [("L14", 10, 90000, 4000, 1200, 3400, 1, 2)]

    out = summary(_Cur(), days=7)
    assert out["ok"] is True
    assert out["coverage"]["layers_recording"] == 1
    assert out["coverage"]["llm_modules_in_tree"] > 1
    assert isinstance(out["coverage"]["instrumented"], list)
    assert "FLOOR on spend" in out["note"]
    assert "uninstrumented, not free" in out["note"]


def test_truncated_and_failed_calls_count_as_waste():
    """A call that stops at max_tokens paid for every input token and returned
    an unparseable answer — 100% waste, which the retry then doubles."""
    from routes.brain_llm_spend import summary

    class _Cur:
        def execute(self, *a, **k):
            return None

        def fetchall(self):
            return [("L14", 10, 90000, 4000, 1200, 3400, 1, 2)]

    layer = summary(_Cur(), days=7)["layers"][0]
    assert layer["wasted_calls"] == 3
    assert layer["p95_ms"] == 3400


def test_unreadable_ledger_is_unmeasured_not_zero():
    from routes.brain_llm_spend import summary

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("no table")

        def fetchall(self):
            raise RuntimeError("no table")

    out = summary(_Boom(), days=7)
    assert out["ok"] is False and "UNMEASURED" in out["error"]


def test_l14_is_not_double_counted():
    """★L14 had bespoke instrumentation before the shared wrapper existed.
    Keeping both would have doubled exactly one layer's numbers — and it would
    have been the one layer with a baseline to compare against."""
    src = _src("routes", "brain_layer14_causal.py")
    assert "_llm_post(" in src
    assert "_spend(_model" not in src, "bespoke ledger calls still present"


def test_every_migrated_site_still_posts_to_anthropic():
    """The rewrite only replaced posts whose FIRST argument is the URL builder.
    If a site lost its URL in the process it would fail at runtime, not here."""
    import pathlib
    root = pathlib.Path(ROOT) / "routes"
    for f in root.glob("*.py"):
        src = f.read_text(encoding="utf-8", errors="replace")
        if "_llm_post(" not in src:
            continue
        assert "anthropic_messages_url()" in src, f"{f.name} lost its URL"


def test_ledger_insert_is_conflict_safe():
    src = _src("routes", "brain_llm_spend.py")
    i = src.index("INSERT INTO brain_llm_spend")
    assert "ON CONFLICT DO NOTHING" in src[i:i + 400]


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.brain_llm_spend import brain_llm_spend_bp" in src
    assert "app.register_blueprint(brain_llm_spend_bp)" in src
