"""Intelligence Expansion Master Shell (#31, 2026-07-25) — pins the contract.

The shell exists because expansion capability keeps degrading INVISIBLY:
rerank died the day the embed provider changed (provider-locked gate), the
media-growth shell ticked while its telemetry never landed, lesson corpora
embed with no proof of recall, and prompt-cache spend was assumed, never
measured. Properties worth pinning:

  1. honesty — a lane never reads PASS when it could not check;
  2. the neutral rerank leg is gated exactly as documented (provider !=
     cohere, master toggle wins, own kill switch works) and NEVER overwrites
     the "cosine" field the dedup/similarity gates threshold on;
  3. usage telemetry is best-effort by contract — no DB, no usage, any
     error → silent no-op, never a raise into a brain call;
  4. it is registered, cron-ticked, killable, and its admin responses are
     no-store (the CF stale-admin-board trap).

CI-SAFETY: the unit-tests job installs pytest/requests/flask/pyyaml/
psycopg2-binary only and has NO DATABASE_URL — modules are imported directly
(never via main) and DB-touching paths are exercised only through their
fail-soft contracts.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL = os.path.join(ROOT, "routes", "intelligence_expansion_master_shell.py")
MAIN = os.path.join(ROOT, "main.py")
CRON = os.path.join(ROOT, "routes", "cron_heartbeat.py")
RAG = os.path.join(ROOT, "routes", "brain_rag.py")
DRIVER = os.path.join(ROOT, "routes", "brain_lane_driver.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import intelligence_expansion_master_shell as m
    return m


@pytest.fixture(scope="module")
def rag():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import brain_rag as m
    return m


# ── wiring ────────────────────────────────────────────────────────────

def test_shell_registered_in_main():
    src = _read(MAIN)
    assert "intelligence_expansion_master_shell_bp" in src
    assert "register_blueprint(intelligence_expansion_master_shell_bp)" in src


def test_shell_cron_ticked_and_killable():
    src = _read(CRON)
    assert "/api/v1/admin/intelligence-expansion/master-tick" in src
    assert "INTEL_EXPANSION_SHELL_DISABLE" in src


def test_shell_responses_are_no_store():
    # ★CF caches admin GETs (brain-ascension stale-board trap) — every
    # response path must set no-store.
    src = _read(SHELL)
    assert "no-store" in src


def test_shell_beats_the_ledger():
    src = _read(SHELL)
    assert "intelligence-expansion-shell-daily" in src
    assert "/api/v1/admin/ingest-runs/beat" in src


# ── honesty: never green-by-silence ──────────────────────────────────

def test_lane_verdict_indeterminate_critical_is_question_mark(shell):
    checks = [shell._check("x", "x", None, "unreachable", critical=True)]
    assert shell._lane_verdict(checks) == "?"


def test_lane_verdict_any_false_is_fail(shell):
    checks = [shell._check("a", "a", True, "ok", critical=True),
              shell._check("b", "b", False, "bad", critical=False)]
    assert shell._lane_verdict(checks) == "FAIL"


def test_lane_verdict_pass_requires_affirmative_criticals(shell):
    checks = [shell._check("a", "a", True, "ok", critical=True),
              shell._check("b", "b", None, "info-only", critical=False)]
    assert shell._lane_verdict(checks) == "PASS"


def test_crashed_lane_renders_indeterminate_not_pass(shell):
    def boom():
        raise RuntimeError("lane exploded")
    checks = shell._safe_lane(boom)
    assert shell._lane_verdict(checks) == "?"


# ── neutral rerank gating ────────────────────────────────────────────

def test_neutral_rerank_on_for_non_cohere_by_default(rag, monkeypatch):
    monkeypatch.setattr(rag, "_embed_provider", lambda: "mistral")
    monkeypatch.delenv("BRAIN_RAG_RERANK", raising=False)
    monkeypatch.delenv("BRAIN_RAG_RERANK_NEUTRAL", raising=False)
    assert rag._neutral_rerank_on() is True


def test_neutral_rerank_defers_to_cohere_cross_encoder(rag, monkeypatch):
    monkeypatch.setattr(rag, "_embed_provider", lambda: "cohere")
    assert rag._neutral_rerank_on() is False


def test_neutral_rerank_master_toggle_wins(rag, monkeypatch):
    monkeypatch.setattr(rag, "_embed_provider", lambda: "mistral")
    monkeypatch.setenv("BRAIN_RAG_RERANK", "0")
    assert rag._neutral_rerank_on() is False


def test_neutral_rerank_own_kill_switch(rag, monkeypatch):
    monkeypatch.setattr(rag, "_embed_provider", lambda: "mistral")
    monkeypatch.delenv("BRAIN_RAG_RERANK", raising=False)
    monkeypatch.setenv("BRAIN_RAG_RERANK_NEUTRAL", "0")
    assert rag._neutral_rerank_on() is False


def test_retrieve_context_wires_the_neutral_leg():
    src = _read(RAG)
    assert "_neutral_rerank_on()" in src
    assert "_lexical_rerank(query, base" in src


# ── neutral rerank behavior ──────────────────────────────────────────

def _mk(text, cosine):
    return {"source_table": "t", "source_id": "s", "kind": "k",
            "text": text, "score": cosine, "cosine": cosine}


def test_lexical_rerank_preserves_cosine_and_marks_leg(rag):
    base = [_mk("ercot grid headroom and interconnection queue", 0.80),
            _mk("unrelated press release about a gala dinner", 0.80)]
    out = rag._lexical_rerank("ercot headroom", base, 2)
    assert all(d["cosine"] == 0.80 for d in out), \
        "cosine is the gate contract — the blend must never overwrite it"
    assert all(d.get("_rerank") == "neutral" for d in out)
    assert out[0]["text"].startswith("ercot"), \
        "term-covering doc must outrank the equal-cosine non-covering doc"
    assert out[0]["score"] > out[1]["score"]


def test_lexical_rerank_respects_k_and_never_mutates_input(rag):
    base = [_mk("alpha beta", 0.9), _mk("gamma delta", 0.8),
            _mk("alpha gamma", 0.7)]
    snapshot = [dict(d) for d in base]
    out = rag._lexical_rerank("alpha", base, 2)
    assert len(out) == 2
    assert base == snapshot, "input dicts must not be mutated"


def test_lexical_rerank_failsoft_on_empty_query(rag):
    base = [_mk("alpha", 0.9), _mk("beta", 0.8)]
    out = rag._lexical_rerank("", base, 1)
    assert out == base[:1]


def test_lexical_rerank_bonus_is_bounded(rag):
    # Full coverage adds at most 0.08 — the blend re-orders candidates, it
    # must not let a lexical match overrule the embedding wholesale.
    base = [_mk("ercot", 0.60), _mk("totally different topic", 0.70)]
    out = rag._lexical_rerank("ercot", base, 2)
    assert out[0]["text"] == "totally different topic", \
        "a 0.10 cosine gap must survive a full-coverage lexical bonus"


# ── usage telemetry contract ─────────────────────────────────────────

def test_record_llm_usage_noop_without_db(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import brain_llm_structured as m
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    # Must be a silent no-op — never a raise into a brain call.
    assert m.record_llm_usage("t", "claude-fable-5",
                              {"usage": {"input_tokens": 1}}) is None


def test_record_llm_usage_noop_without_usage_block(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import brain_llm_structured as m
    # Even WITH a DB url set, an empty usage block must return before any
    # connection attempt (order matters: usage check precedes connect).
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid.invalid/x")
    assert m.record_llm_usage("t", "m", {}) is None
    assert m.record_llm_usage("t", "m", None) is None


def test_lane_driver_wired_for_usage_capture():
    src = _read(DRIVER)
    assert "record_llm_usage" in src
