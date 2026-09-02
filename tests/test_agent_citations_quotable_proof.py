"""Guards for the citations endpoint's quotable line — routes/agent_citations
(2026-09-02).

★ THE DEFECT THIS GUARD EXISTS TO RETIRE

GET /api/v1/agents/citations publishes `agent_quotable_proof`, "the
cite-this-and-feel-confident line that OTHER agents fetching this URL will
quote back to their users". Measured 2026-09-02 00:23Z it read:

    "DC Hub's MCP server is actively used by Claude (Anthropic), Claude
     Desktop, Grok (xAI), Cursor, ChatGPT (OpenAI), and GitHub Copilot ..."

while the by_platform rows beneath it said:

    Claude Desktop    calls_30d 0   last_seen 2026-07-03
    Cursor            calls_30d 0   last_seen 2026-07-28
    GitHub Copilot    calls_30d 0   last_seen 2026-08-02

The names were picked as by_platform[:6] — sorted by LIFETIME total — with no
recency gate at all. And ChatGPT's 33 calls/30d included `GPTBot/1.1`, which
is OpenAI's crawler, classified as the assistant.

Pure functions: no DB, no network. The module imports cleanly without one.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from routes.agent_citations import (  # noqa: E402
    _classify_ua, _quotable_proof, _used_in_last_30d,
)


def _p(name, role, total, d30, d7=0):
    return {"platform": name, "role": role, "calls_total": total,
            "calls_30d": d30, "calls_7d": d7}


# The live by_platform ordering on 2026-09-02, lifetime-sorted.
_LIVE = [
    _p("Claude (Anthropic)", "AI assistant", 9000, 644, 277),
    _p("Claude Desktop", "MCP client", 5000, 0),
    _p("Grok (xAI)", "AI assistant", 900, 91, 33),
    _p("Cursor", "AI IDE", 600, 0),
    _p("ChatGPT (OpenAI)", "AI assistant", 400, 33, 0),
    _p("GitHub Copilot", "AI coding", 300, 0),
    _p("Amazon AI", "AI crawler", 50, 3),
    _p("Gemini (Google)", "AI assistant", 40, 0),
    _p("Perplexity", "AI search", 30, 0),
]
_TOTALS = {"unique_platforms": 9, "total_calls_30d": 771}


def test_platforms_with_no_calls_in_30d_are_never_named():
    """★ THE REGRESSION."""
    names = [p["platform"] for p in _used_in_last_30d(_LIVE)]
    assert names == ["Claude (Anthropic)", "Grok (xAI)", "ChatGPT (OpenAI)"]
    line = _quotable_proof(_LIVE, _TOTALS)
    for stale in ("Claude Desktop", "Cursor", "GitHub Copilot", "Gemini", "Perplexity"):
        assert stale not in line, f"{stale} has calls_30d 0 and is named: {line}"


def test_the_sentence_says_which_window_it_describes():
    line = _quotable_proof(_LIVE, _TOTALS)
    assert line.startswith("DC Hub's MCP server was used in the last 30 days by ")
    assert "actively used" not in line
    assert "771 tool calls" in line
    # the platform count is the 30d one, and the lifetime one is labelled
    assert "across 3 AI platforms" in line
    assert "9 distinct platforms all-time" in line
    assert line.endswith("CC-BY-4.0.")


def test_a_crawler_is_kept_in_the_receipts_but_not_in_the_claim():
    """Amazonbot made 3 calls in 30d. It is in by_platform (the receipts are
    complete) and NOT in "used by" — a crawler is not a platform acting for
    a user."""
    assert "Amazon AI" not in [p["platform"] for p in _used_in_last_30d(_LIVE)]
    assert "Amazon" not in _quotable_proof(_LIVE, _TOTALS)


def test_lifetime_order_is_preserved_among_the_named():
    """Recency is a GATE, not a re-sort — the biggest current user still leads."""
    names = [p["platform"] for p in _used_in_last_30d(_LIVE)]
    assert names[0] == "Claude (Anthropic)"


def test_fewer_than_three_names_still_reads_honestly():
    two = [_p("Claude (Anthropic)", "AI assistant", 9000, 644),
           _p("Grok (xAI)", "AI assistant", 900, 91),
           _p("Cursor", "AI IDE", 600, 0)]
    line = _quotable_proof(two, {"unique_platforms": 3, "total_calls_30d": 735})
    assert line.startswith("DC Hub's MCP server was used in the last 30 days by Claude (Anthropic), Grok (xAI)")
    assert "Cursor" not in line


def test_nothing_used_in_30d_publishes_no_claim_at_all():
    """An empty string beats a stale sentence — the consumer can branch."""
    dead = [_p("Cursor", "AI IDE", 600, 0), _p("Claude Desktop", "MCP client", 5000, 0)]
    assert _quotable_proof(dead, {"unique_platforms": 2, "total_calls_30d": 0}) == ""


def test_a_malformed_count_is_skipped_not_named():
    weird = [_p("X", "AI assistant", 10, "many"), _p("Y", "AI assistant", 5, None)]
    assert _used_in_last_30d(weird) == []


def test_gptbot_is_a_crawler_not_chatgpt():
    """OpenAI's index/training crawler was folded into "ChatGPT (OpenAI)"."""
    name, role = _classify_ua("Mozilla/5.0 AppleWebKit/537.36 (compatible; GPTBot/1.1; +https://openai.com/gptbot)")
    assert role == "AI crawler", (name, role)
    assert name != "ChatGPT (OpenAI)"
    assert "GPTBot" in name
    # and the assistant itself still classifies as the assistant
    assert _classify_ua("ChatGPT-User/1.0") == ("ChatGPT (OpenAI)", "AI assistant")
    assert _classify_ua("chatgpt") == ("ChatGPT (OpenAI)", "AI assistant")


def test_the_gather_path_uses_the_gated_helper():
    """The pure helper is only worth anything if the endpoint calls it."""
    src = open(os.path.join(REPO_ROOT, "routes", "agent_citations.py"),
               encoding="utf-8").read()
    body = src[src.index("def _gather_citations"):]
    assert "_quotable_proof(out[\"by_platform\"], out[\"totals\"])" in body
    assert "by_platform[:6]" not in body, "the ungated slice is back"
    assert "actively used by" not in body, "the old wording is back in the endpoint"
