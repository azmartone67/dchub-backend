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

import pytest  # noqa: E402

# routes.wins_poster imports flask, which the pure-pytest CI job (pip install
# pytest, nothing else) does not provide. importorskip lets this suite SKIP
# cleanly under CI instead of erroring collection and aborting the whole run.
wp = pytest.importorskip("routes.wins_poster")  # noqa: E402


def test_fence_self_check_rejects_banned_strings():
    assert wp._fence_safe("DC Hub tracks 3,000+ verified facilities") is True
    assert wp._fence_safe("Tracks $324B+ in deals") is False
    assert wp._fence_safe("50,000 data centers indexed") is False
    assert wp._fence_safe("286 markets covered") is False


def test_fence_catches_hyphen_and_plaintext_bypass_variants():
    """The fence must not be defeated by spacing/hyphen/plain-text variants — an
    LLM-drafted ship-win post could phrase a banned figure any way."""
    for bad in ("we track 50,000+ data-center sites",
                "worth 324 billion dollars in deals",
                "across 286-markets nationwide",
                "now covering 340+ markets",
                "a $324B pipeline"):
        assert wp._fence_safe(bad) is False, f"fence must reject: {bad!r}"
    # …without over-matching legitimate copy (incl. the real ship-win drafts).
    for ok in ("21,000 facilities across 178 countries",
               "232 DCPI markets tracked",
               "real-time grid headroom for 38 ISO markets — now live",
               "roughly 300 power markets"):
        assert wp._fence_safe(ok) is True, f"fence must allow: {ok!r}"


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


# ── ship-win detector (r-ship-wins) ──────────────────────────────────────────
def test_ship_metric_accepts_concrete_capability():
    # quantified / concrete shipped capabilities ARE postable
    assert wp._extract_ship_metric(
        "Added real-time grid headroom for 38 ISO markets", "")
    assert wp._extract_ship_metric(
        "Launched per-facility fiber-route API", "Developers can now query lead-in routes.")
    assert wp._extract_ship_metric(
        "Shipped a new tax-incentive dataset", "")


def test_ship_metric_rejects_vague_promo():
    # hype with no concrete capability is NOT an honest analyst post
    assert wp._extract_ship_metric("We revolutionized data-center siting", "") is None
    assert wp._extract_ship_metric("A game-changing update", "Unlocked AI for everyone.") is None
    assert wp._extract_ship_metric("", "") is None


def test_ship_template_is_number_led_and_fence_safe():
    # a numeric ship snippet composes a number-led, fence-safe post (no LLM)
    text = wp.compose_win_post({
        "kind": "ship",
        "win_key": "ship:1:grid-headroom",
        "_metric_snippet": "Real-time grid headroom for 38 ISO markets",
        "_llm_draft": None,
    }, "linkedin")
    assert text, "numeric ship snippet should compose a template post"
    assert wp._number_led(text), "ship post must lead with a number, not a bare year"
    assert wp._fence_safe(text)
    assert "DC Hub" in text


def test_ship_post_rejects_year_led_llm_draft():
    # an LLM draft that opens with a bare year is REJECTED (year is never an honest
    # analyst hook); the composer falls back to the safe capability template, and
    # the year-led draft text is NOT used verbatim.
    year_led = ("2026 was the year DC Hub shipped a fiber-route API. "
                "Source: DC Hub · dchub.cloud")
    text = wp.compose_win_post({
        "kind": "ship",
        "win_key": "ship:2:year-led",
        "_metric_snippet": "Launched per-facility fiber-route API",
        "_llm_draft": year_led,
    }, "linkedin")
    assert text and text != year_led, "year-led draft must not be used verbatim"
    assert not wp._year_led(text), "composed ship post must not lead with a bare year"
    assert wp._fence_safe(text)


def test_year_led_helper():
    assert wp._year_led("2026 was a big year for siting") is True
    assert wp._year_led("38 ISO markets now expose grid headroom") is False
    assert wp._year_led("Real-time grid headroom is now live") is False


def test_ship_post_rejects_banned_figure_in_llm_draft():
    # an LLM draft carrying a retired over-claim is REJECTED by the fence; with a
    # clean numeric snippet, the composer falls back to the safe template instead
    text = wp.compose_win_post({
        "kind": "ship",
        "win_key": "ship:3:banned",
        "_metric_snippet": "Added 38 new ISO regions",
        "_llm_draft": "50,000 data centers now indexed across $324B+ in deals. "
                      "Source: DC Hub · dchub.cloud",
    }, "linkedin")
    assert text, "should fall back to the safe template, not return the banned draft"
    assert wp._fence_safe(text), "composed ship post must never carry a banned over-claim"
    assert "324B" not in text and "50,000" not in text


def test_ship_post_prefers_clean_llm_draft():
    clean = ("38 ISO markets now expose real-time grid headroom in DC Hub. "
             "For capacity planners that compresses a site's time-to-power "
             "question from weeks to seconds.\n\n"
             "Source: DC Hub, the live infrastructure data layer for AI agents · dchub.cloud")
    text = wp.compose_win_post({
        "kind": "ship",
        "win_key": "ship:4:clean",
        "_metric_snippet": "Real-time grid headroom for 38 ISO markets",
        "_llm_draft": clean,
    }, "linkedin")
    assert text == clean, "a number-led, fence-safe LLM draft should be used verbatim"


def test_detect_ship_win_is_in_autopilot_loop():
    # the autopilot orchestrator must actually call detect_ship_win
    import inspect
    src = inspect.getsource(wp.queue_wins)
    assert "detect_ship_win" in src, "queue_wins must include detect_ship_win in its detector loop"


def test_detect_ship_win_dedups_and_drafts_from_stubbed_source(monkeypatch):
    """detect_ship_win pulls ships from a stubbed press_releases source, drafts via
    a stubbed LLM, dedups by slug, and returns the autopilot candidate shape."""
    # stub the LLM so the test is offline + deterministic
    monkeypatch.setattr(
        wp, "_draft_ship_post_llm",
        lambda title, metric: f"{metric} — now live. Source: DC Hub · dchub.cloud")

    class _FakeCur:
        def execute(self, *a, **k): pass
        def fetchall(self):
            return [
                (101, "grid-headroom", "Added real-time grid headroom for 38 ISO markets", ""),
                (101, "grid-headroom", "Added real-time grid headroom for 38 ISO markets", ""),  # dup slug
                (102, "promo-fluff", "We revolutionized everything", "game-changing update"),     # vague → drop
                (103, "fiber-api", "Launched per-facility fiber-route API", "Query lead-in routes."),
            ]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _FakeConn:
        def cursor(self): return _FakeCur()
        def close(self): pass

    monkeypatch.setattr(wp, "_conn", lambda: _FakeConn())
    leads = wp.detect_ship_win()
    keys = [l["win_key"] for l in leads]
    # dup slug collapsed; vague ship dropped; two real ships remain
    assert keys == ["ship:101:grid-headroom", "ship:103:fiber-api"], keys
    for l in leads:
        assert l["kind"] == "ship"
        assert l["cooldown_days"] == 14
        assert l["_metric_snippet"]
        assert l["_llm_draft"]
        # the lead composes into a fence-safe post
        assert wp._fence_safe(wp.compose_win_post(l, "linkedin") or "")


def test_detect_ship_win_silent_without_db(monkeypatch):
    monkeypatch.setattr(wp, "_conn", lambda: None)
    assert wp.detect_ship_win() == []
