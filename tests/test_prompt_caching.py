"""Unit tests for prompt-caching helpers (no network / no API key needed)."""
from utils.anthropic_helper import cached_system
from routes.brain_llm_structured import build_messages_body


def _block(system):
    """The single text block cached_system should produce for a str."""
    assert isinstance(system, list) and len(system) == 1
    b = system[0]
    assert b["type"] == "text"
    assert b["cache_control"]["type"] == "ephemeral"
    return b


def test_wraps_static_str():
    out = cached_system("You are the DC Hub brain.")
    b = _block(out)
    assert b["text"] == "You are the DC Hub brain."
    assert "ttl" not in b["cache_control"]


def test_ttl_opt_in():
    b = _block(cached_system("x" * 10, ttl="1h"))
    assert b["cache_control"]["ttl"] == "1h"


def test_noop_on_non_str_and_empty():
    # idempotent / safe: leave these untouched so double-wrap or pre-built
    # block-lists don't get corrupted.
    assert cached_system("") == ""
    assert cached_system(None) is None
    already = [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]
    assert cached_system(already) is already


def test_build_messages_body_caches_system_by_default():
    body, applied = build_messages_body(
        "claude-opus-4-8", "STATIC BRAIN CHARTER",
        [{"role": "user", "content": "go"}], max_tokens=100)
    b = _block(body["system"])
    assert b["text"] == "STATIC BRAIN CHARTER"


def test_build_messages_body_opt_out_leaves_string():
    body, _ = build_messages_body(
        "claude-opus-4-8", "PER-REQUEST-CHANGES", [], max_tokens=10,
        cache_system=False)
    assert body["system"] == "PER-REQUEST-CHANGES"  # untouched str, no cache write
