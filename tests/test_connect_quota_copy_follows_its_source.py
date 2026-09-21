"""/connect/<client>: every quota figure on an install page comes from where it is set.

r-connect-quota-copy (2026-09-15). #4610 made steps 1 and 4 read the trial terms
from routes/auto_trial.py on every render. Three quota claims on the same pages
were still typed by hand:

  * The Gemini snippet's header line said the key "unlocks 50/day". An unbound
    key gets TRIAL_FREE_CALLS_UNBOUND calls in all before validate_trial_key
    refuses it (bind_email_required); TRIAL_DAILY_CALLS is a bound key's quota.
    The page now puts step 4's terms under that line, in the <pre> and in
    RAW_SNIPPET, the text the page script swaps the minted key into.
  * Step 4 said Pro "gets you unlimited daily quota", and the Pro Monthly tile
    "Same unlimited access". tier_registry gives Pro a finite mcp_daily, which
    step 4 now quotes through canon, naming the MCP lane as /connect's card does.
  * The page script's tier line fell back to typed figures. Those checks run the
    script in node: tests/test_connect_gated_mint_shows_bind_step.py, section 5.

★ 2026-09-21 (P0-D, frontend#1535): step 4 sells the plans agents buy, the $10
pack and Developer, and no longer tiles Pro. The quota it quotes is Developer's
MCP quota, read through canon from tier_registry, and it is pinned the same way
Pro's was: patched to values the defaults never take, between two renders.

Rendered with mc._render_page. Figures are patched to values the defaults never
take, and patched again between two renders, so a typed figure, or one read once
and kept, goes red.

Run:  python3 -m pytest tests/test_connect_quota_copy_follows_its_source.py -v
"""

from __future__ import annotations

import html as htmllib
import json
import re
import sys

import pytest

import canonical_stats as cs
import routes.auto_trial as at
import routes.mcp_connect as mc
import tier_registry

ALL_CLIENTS = sorted(mc._CLIENTS)
TERMS_A = (7, 23, 61, 9)      # free calls, unbound a day, bound a day, trial days
TERMS_B = (4, 19, 83, 11)
# "N/day", "N requests/day", "N req/day", "N calls/day".
PER_DAY = re.compile(r"(\d[\d,]*)\s*(?:requests?|req|calls?)?\s*/\s*day", re.I)


@pytest.fixture
def stats_state():
    """Restore canonical_stats' module cache, which rendering reads."""
    prev_cache, prev_ts, prev_live = cs._cache, cs._cache_ts, set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


def _terms(monkeypatch, free, daily_unbound, daily_bound, days):
    monkeypatch.setattr(at, "TRIAL_FREE_CALLS_UNBOUND", free)
    monkeypatch.setattr(at, "TRIAL_DAILY_UNBOUND", daily_unbound)
    monkeypatch.setattr(at, "TRIAL_DAILY_CALLS", daily_bound)
    monkeypatch.setattr(at, "TRIAL_DAYS", days)


def _pre(page):
    """The install snippet as the <pre> shows it."""
    m = re.search(r'<pre id="snippet-body">(.*?)</pre>', page, re.S)
    assert m, "the page has no snippet <pre>"
    return htmllib.unescape(m.group(1))


def _js_json(page, name):
    """The JSON literal the page script's `const <name>` is set to."""
    m = re.search(rf"^const {name} = (.*);$", page, re.M)
    assert m, f"the page script has no const {name}"
    return json.loads(m.group(1))


def _step4(page):
    """Step 4's paragraph as read: tags stripped, whitespace collapsed."""
    p = page.index("<p", page.index("<!-- STEP 4:"))
    copy = page[page.index(">", p) + 1:page.index("</p>", p)]
    return " ".join(re.sub(r"<[^>]+>", "", copy).split())


# ── 1. the Gemini snippet ────────────────────────────────────────────────
def test_the_gemini_snippet_states_the_terms_auto_trial_enforces(monkeypatch, stats_state):
    header = "Header (optional):  X-API-Key: " + mc.TRIAL_KEY_SENTINEL
    # Two renders, two sets of terms: the second render has to see the second set.
    for free, daily_unbound, daily_bound, days in (TERMS_A, TERMS_B):
        _terms(monkeypatch, free, daily_unbound, daily_bound, days)
        page = mc._render_page("gemini", 4242)
        for where, served in (("<pre>", _pre(page)), ("RAW_SNIPPET", _js_json(page, "RAW_SNIPPET"))):
            assert served.splitlines()[-3:] == [
                header,
                f"Without an email, a trial key gets {free} free calls.",
                f"Bind an email and the same key keeps working, at {daily_bound} "
                f"requests/day for the rest of its {days}-day trial.",
            ], f"/connect/gemini {where}: {served!r}"


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_the_pre_and_the_page_script_carry_the_same_snippet(client, monkeypatch, stats_state):
    _terms(monkeypatch, *TERMS_A)
    page = mc._render_page(client, 4242)
    assert _pre(page) == _js_json(page, "RAW_SNIPPET")


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_a_daily_figure_in_a_snippet_is_the_rendered_bound_quota(client, monkeypatch, stats_state):
    _terms(monkeypatch, *TERMS_A)
    snippet = _pre(mc._render_page(client, 4242))
    stated = {int(n.replace(",", "")) for n in PER_DAY.findall(snippet)}
    if mc._CLIENTS[client].get("snippet_trial_terms"):
        assert stated == {TERMS_A[2]}, snippet
    else:
        assert not stated, f"/connect/{client}'s snippet types a daily figure: {snippet!r}"


def test_the_snippet_says_what_step_four_says(monkeypatch, stats_state):
    # Free calls tuned above the unbound daily cap, so both carry the cap clause.
    _terms(monkeypatch, 40, 12, 61, 9)
    page = mc._render_page("gemini", 4242)
    terms = " ".join(_pre(page).splitlines()[-2:])
    assert "40 free calls, at most 12 a day." in terms, terms
    assert terms in _step4(page), (terms, _step4(page))


def test_unreadable_terms_leave_the_snippet_without_a_figure(monkeypatch, stats_state):
    # None in sys.modules makes the render path's import of routes.auto_trial raise.
    monkeypatch.setitem(sys.modules, "routes.auto_trial", None)
    page = mc._render_page("gemini", 4242)
    for served in (_pre(page), _js_json(page, "RAW_SNIPPET")):
        assert "Header (optional):  X-API-Key: " + mc.TRIAL_KEY_SENTINEL in served, served
        assert "a limited number of free calls" in served, served
        assert "the same key keeps working" in served, served
        assert not re.search(r"\d+\s*(?:free calls|requests/day|-day)", served), served
    assert _js_json(page, "TRIAL_TERMS") is None, "the page script must get no terms to fall back on"


# ── 2. Pro's quota in step 4 ─────────────────────────────────────────────
def test_tier_registry_gives_pro_a_finite_mcp_quota():
    """The claim this replaces was "unlimited", which is wrong while this holds."""
    n = tier_registry.calls_per_day("pro")
    assert isinstance(n, int) and n > 0, n


def test_step_four_quotes_developer_s_mcp_quota_from_tier_registry(monkeypatch, stats_state):
    want = f"for {tier_registry.calls_per_day('developer'):,} MCP calls/day on every tool"
    assert want in _step4(mc._render_page("chatgpt", 4242))
    for n in (3141, 27182):
        monkeypatch.setitem(tier_registry.TIER_LIMITS["developer"], "mcp_daily", n)
        copy = _step4(mc._render_page("chatgpt", 4242))
        assert f"for {n:,} MCP calls/day on every tool" in copy, copy


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_no_install_page_calls_pro_unlimited(client, stats_state):
    page = mc._render_page(client, 4242)
    # Floor: the upgrade copy and the paid tiles are there to be read.
    assert "MCP calls/day on every tool" in _step4(page) and 'id="upg-developer"' in page
    assert "unlimited" not in page.lower(), (
        f"/connect/{client} calls Pro unlimited; tier_registry gives it "
        f"{tier_registry.calls_per_day('pro'):,} MCP calls a day")
