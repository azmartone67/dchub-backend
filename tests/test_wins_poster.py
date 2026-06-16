"""Guards for the DC Hub Media wins auto-drafter (r-wins, 2026-06-16).

Locks the honesty + safety contract so a regression can't ship a fabricated or
over-claiming "win" post:
  • every composed template is NUMBER-LED (analyst voice, not a headline scrape)
  • every composed template is FENCE-SAFE (no $324B / 50,000 / inflated-market
    over-claims — the same family tests/test_honest_numbers.py bans)
  • the fence self-check actually rejects a banned string
  • milestone numbers come from canonical_stats phrase helpers (citation-safe)

DB-free (canonical_stats falls back to citation-safe floors without DATABASE_URL),
so it runs in CI.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes import wins_poster as wp  # noqa: E402


def test_fence_self_check_rejects_banned_strings():
    assert wp._fence_safe("DC Hub tracks 3,000+ verified facilities") is True
    assert wp._fence_safe("Tracks $324B+ in deals") is False
    assert wp._fence_safe("50,000 data centers indexed") is False
    assert wp._fence_safe("286 markets covered") is False


def test_milestone_template_is_number_led_and_fence_safe():
    text = wp.compose_win_post({"kind": "milestone", "metric": "facilities"}, "linkedin")
    assert text, "milestone template should compose"
    assert wp._fence_safe(text), "milestone post must not carry a banned over-claim"
    # number-led (analyst voice): a digit appears in the opening
    assert any(ch.isdigit() for ch in text[:80])


def test_agent_traction_template_is_number_led_and_fence_safe():
    text = wp.compose_win_post(
        {"kind": "agent_traction", "headline_num": 42}, "linkedin")
    assert text, "agent_traction template should compose"
    assert "42" in text[:60], "traction post must lead with the distinct-agent count"
    assert wp._fence_safe(text)


def test_citation_template_is_fence_safe():
    text = wp.compose_win_post(
        {"kind": "citation", "engine": "Claude", "prompt": "where to build 100MW"},
        "linkedin")
    assert text and wp._fence_safe(text)
    assert "Claude" in text


def test_internal_filter_catches_synthetic_traffic():
    # the agent-traction count must NEVER credit internal/probe traffic as an agent
    for junk in ("dchub-selfheal", "DCHub-DailyDigest/1.0", "uptime-probe",
                 "python-requests/2.31", "Render/1.0", "mcp-test-sweep"):
        assert wp._INTERNAL_RE.search(junk), f"internal filter must catch {junk!r}"
    # a real agent UA must NOT be filtered
    for real in ("Claude-User", "ChatGPT", "cursor-mcp", "perplexity-bot-research"):
        assert not wp._INTERNAL_RE.search(real), f"real agent {real!r} wrongly filtered"


def test_default_is_review_queue_not_autosend():
    # the module-level default must be review-queue (draft), never auto-send,
    # unless the env flag is explicitly set.
    assert wp._AUTOPILOT is False or os.environ.get("WINS_POSTER_AUTOPILOT_ENABLED")
