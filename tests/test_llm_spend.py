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


def test_l14_records_both_success_and_failure():
    """★A FAILED call still spent input tokens and wall-clock. Not recording it
    makes the ledger flatter than reality and hides the retry-doubling this
    very loop does on a bad model pin."""
    src = _src("routes", "brain_layer14_causal.py")
    assert "_spend(_model, None, _ms, ok=False" in src
    assert "_spend(_model, body, _ms, ok=True" in src
    assert "from routes.brain_llm_spend import record" in src


def test_ledger_insert_is_conflict_safe():
    src = _src("routes", "brain_llm_spend.py")
    i = src.index("INSERT INTO brain_llm_spend")
    assert "ON CONFLICT DO NOTHING" in src[i:i + 400]


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.brain_llm_spend import brain_llm_spend_bp" in src
    assert "app.register_blueprint(brain_llm_spend_bp)" in src
